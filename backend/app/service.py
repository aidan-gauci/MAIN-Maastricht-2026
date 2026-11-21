import json
import logging
import inspect
import os
from collections import Counter
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

from .audio import audio_file, create_audio
from .config import CORPUS, DATA, MODE, STYLES, EUROPE_COUNTRIES
from . import pipeline
from .pipeline import narrate, prepare, prepare_ranking
from .providers import Providers
from .storage import Store
from .summaries import prepare_summaries, select_summaries
from .translations import translate_corpus
from .threads import assign_threads

STAGES = ("ingestion", "story_extraction", "script_generation", "speech_generation", "label_translation")
PREPARATION_STAGES = ("ingestion", "story_extraction", "label_translation")
# Average pace of a narrated briefing. Narration is built only from verified figures and never padded,
# so the briefing reports how long it will actually run next to the length the listener asked for.
SPOKEN_WORDS_PER_MINUTE = 150
UTC = timezone.utc


class NoRelevantStories(ValueError):
    pass


def utc_now():
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def schedule_lead() -> timedelta:
    """How long before delivery a scheduled briefing is generated: 30 minutes, or BRIEFING_SCHEDULE_LEAD_MINUTES.

    A short lead (1 to 5 minutes) lets a demo show a delivery arriving; production keeps the default.
    """
    try:
        minutes = int(os.getenv("BRIEFING_SCHEDULE_LEAD_MINUTES", "30"))
    except ValueError:
        minutes = 30
    return timedelta(minutes=min(120, max(1, minutes)))


def next_occurrence(schedule: dict, after: datetime | None = None) -> dict[str, str]:
    """Return the next delivery whose generation window (see schedule_lead) is still ahead."""
    zone = ZoneInfo(schedule["timezone"])
    now = (after or utc_now()).astimezone(UTC)
    local_now = now.astimezone(zone)
    hour, minute = (int(part) for part in schedule["local_time"].split(":"))
    day = local_now.date()
    for _ in range(15):
        if schedule["cadence"] == "weekly" and day.weekday() != schedule["weekday"]:
            day += timedelta(days=1)
            continue
        naive = datetime(day.year, day.month, day.day, hour, minute)
        candidate = naive.replace(tzinfo=zone)
        # Move a nonexistent spring-forward wall time to the first valid minute.
        while candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
            naive += timedelta(minutes=1)
            candidate = naive.replace(tzinfo=zone)
        delivery = candidate.astimezone(UTC)
        lead = schedule_lead()
        if delivery - lead > now:
            return {"generation_at": iso_utc(delivery - lead), "delivery_at": iso_utc(delivery)}
        day += timedelta(days=1)
    raise ValueError("Could not find a future schedule occurrence")


def schedule_envelope(schedule: dict) -> dict[str, str | int]:
    return {"schedule_id": schedule["schedule_id"], "schedule_version": schedule["schedule_version"],
            "generation_at": schedule["generation_at"], "delivery_at": schedule["delivery_at"]}


def scheduled_run_id(envelope: dict) -> str:
    return uuid5(NAMESPACE_URL, "briefing-run:" + ":".join(str(envelope[key]) for key in ("schedule_id", "schedule_version", "generation_at"))).hex


def local_delivery_date(schedule: dict, delivery_at: str) -> str:
    delivery = datetime.fromisoformat(delivery_at.replace("Z", "+00:00"))
    return delivery.astimezone(ZoneInfo(schedule["timezone"])).date().isoformat()


def _region_matches(story: dict, region: str) -> bool:
    if region == "all":
        return True
    for article in story.get("articles", []):
        explicit_region = article.get("region")
        country = article.get("country")
        if region == "Europe" and (explicit_region == "Europe" or country in EUROPE_COUNTRIES):
            return True
        if region != "Europe" and (explicit_region == region or country == region):
            return True
    return False


def rank_for_listener(stories: list[dict], profile: dict, *, feedback_events=None,
                      spoken_fact_ids=None, region="all", scheduled=False):
    """Bridge the configured pipeline API and the baseline two-argument ranker."""
    ranker = pipeline.rank
    parameters = inspect.signature(ranker).parameters
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    kwargs = {"feedback_events": feedback_events, "spoken_fact_ids": spoken_fact_ids, "region": region}
    ranked = ranker(stories, profile, **{key: value for key, value in kwargs.items() if accepts_kwargs or key in parameters})
    if "region" not in parameters and not accepts_kwargs and region != "all":
        ranked = [story for story in ranked if _region_matches(story, region)]
    if scheduled and "spoken_fact_ids" not in parameters and not accepts_kwargs and spoken_fact_ids:
        remembered = set(spoken_fact_ids)
        ranked = [
            {**story, "facts": [fact for fact in story.get("facts", []) if fact.get("id") not in remembered]}
            for story in ranked
        ]
        ranked = [story for story in ranked if story["facts"]]
    if scheduled and "region" not in parameters and not accepts_kwargs:
        ranked = [story for story in ranked if story.get("score_components", {}).get("topic_match", 0) > 0]
    return ranked


def rank_profiles(stories: list[dict], profiles: list[dict]) -> dict[str, list[dict]]:
    ranker = getattr(pipeline, "rank_profiles", None)
    if ranker:
        return ranker(stories, profiles)
    return {profile["id"]: rank_for_listener(stories, profile) for profile in profiles}


def next_schedule_times(schedule: dict, delivery_at: str) -> dict[str, str]:
    due_at = datetime.fromisoformat(delivery_at.replace("Z", "+00:00"))
    return next_occurrence(schedule, max(due_at, utc_now()))


def get_store():
    directory = DATA / CORPUS / MODE
    return Store(directory / "briefings.sqlite3", directory / "audio")


def get_providers(store, *, record_usage=None):
    return Providers(MODE, record_usage or store.record_usage)


def prepare_corpus(store, articles, *, allow_fallback=True, report=None):
    """Prepare and store a corpus; on any error the previously imported corpus is kept.

    With ``allow_fallback=False`` a failed live extraction aborts instead of silently storing
    the local heuristic clusters. A provider reply that stays unusable for a single article is not
    a failure: that article uses the heuristic and is counted in ``report["heuristic_fallback_articles"]``.
    """
    if not articles:
        raise ValueError("Import requires at least one article")
    duplicates = [key for key, count in Counter(row["article_id"] for row in articles).items() if count > 1]
    if duplicates:
        raise ValueError("Import requires unique article IDs; repeated: " + ", ".join(sorted(duplicates)[:5]))
    # Buffer usage from this import until its corpus and summaries can be committed together.
    usage = []

    def record_usage(stage, **metrics):
        usage.append(dict(stage=stage, **metrics))

    providers = get_providers(store, record_usage=record_usage)
    stories = prepare(articles, providers)
    if getattr(providers, "extraction_failed", False):
        if not allow_fallback:
            raise ValueError("Live extraction failed; the previous corpus was kept. Retry, or pass --allow-offline-fallback")
        logging.getLogger(__name__).warning("Live extraction failed; stored the offline heuristic clusters")
    if not stories:
        raise ValueError("Preparation produced no stories")
    # Provider extraction is the paid, slow step. Keep its result beside the database until the corpus is
    # stored, so a failure in a later step does not throw that work away.
    checkpoint = store.path.with_name("extraction-checkpoint.json") if getattr(providers, "live", False) else None
    if checkpoint:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps({"stories": stories, "usage": usage}), encoding="utf-8")
    by_id = {article["article_id"]: article for article in articles}
    for story in stories:
        for source in story.get("articles", []):
            article = by_id.get(source.get("article_id"), {})
            for key in ("region", "country"):
                if article.get(key):
                    source[key] = article[key]
    assign_threads(stories)
    # Headlines and measure names for listeners in the corpus's other languages (live only; checked labels).
    translate_corpus(stories, providers)
    store.replace_corpus(articles, stories, prepare_summaries(stories), preparation_usage=usage)
    if checkpoint:
        checkpoint.unlink(missing_ok=True)
    if report is not None:
        count = getattr(providers, "heuristic_fallback_articles", 0)
        report["heuristic_fallback_articles"] = count if isinstance(count, int) else 0
    return stories


MAX_LISTED_TOPICS = 36
SUBJECT_AREA_MIN_STORIES = 5


def list_topics(stories):
    """The topics a listener can choose from.

    A small corpus offers every topic. A live import tags its stories with hundreds of one-off subjects, so
    there the choice is the subject areas that several speakable stories report on, then the most common
    tagged subjects, most frequent first. Stories withheld from briefings (advice, unverified) do not count.
    """
    every = sorted({topic for story in stories for topic in story.get("topics", [])})
    if len(every) <= MAX_LISTED_TOPICS:
        return every
    speakable = [story for story in stories if pipeline._trust_eligible(story)[0]]
    texts = [f"{story.get('title', '')} " + " ".join(str(fact.get("evidence", "")) for fact in story.get("facts", []))
             for story in speakable]
    areas = Counter({area: sum(bool(pattern.search(text)) for text in texts)
                     for area, pattern in pipeline._TOPIC_PATTERNS.items()})
    tagged = Counter(topic for story in speakable for topic in dict.fromkeys(story.get("topics", [])))
    listed = [area for area, count in areas.most_common() if count >= SUBJECT_AREA_MIN_STORIES]
    seen = {topic.casefold() for topic in pipeline._TOPIC_PATTERNS}
    for topic, count in tagged.most_common():
        if len(listed) >= MAX_LISTED_TOPICS or count < 3:
            break
        # One entry per subject: not a tag a subject area already covers ("revenue", "technologie"), nor the
        # same word again in the singular or in another language of the corpus.
        key = _SAME_TOPIC.get(topic.casefold(), topic.casefold())
        if key in seen or key.rstrip("s") in seen or key + "s" in seen or topic == "news":
            continue
        if any(pattern.search(topic) for pattern in pipeline._TOPIC_PATTERNS.values()):
            continue
        seen.add(key)
        listed.append(topic)
    return listed


_SAME_TOPIC = {"aandelen": "stocks", "aktien": "stocks", "aktie": "stocks", "aandeel": "stocks", "financiën": "finance",
               "finanzen": "finance", "wirtschaft": "economy", "economie": "economy", "ki": "ai", "markt": "markets",
               "märkte": "markets", "markten": "markets", "investitionen": "investing", "investeringen": "investing"}


def wildcard_profile(wildcard):
    return dict(
        id="wildcard",
        name="Wildcard listener",
        topic_weights=dict.fromkeys(wildcard.topics, 1.0),
        excluded_topics=[],
        expertise=wildcard.expertise,
        language=wildcard.language,
        duration=wildcard.duration,
        style=STYLES[wildcard.expertise],
    )


def _build_recommendations(stories, summaries):
    return prepare_ranking(stories), summaries or prepare_summaries(stories), list_topics(stories), bool(stories)


def recommendation_topics(store):
    return store.recommendation_snapshot(_build_recommendations)[2]


def _briefing_story(story, language):
    """Story entry of a briefing. German and Dutch briefings head it with that language's edition of the title.

    ``title_language`` is set only when the title really is in the listener's language, so the voice reads
    the headline only then. Without such an edition the story title stays as the transcript heading.
    """
    title, title_language = pipeline.localized_headline(story, language)
    entry = {"id": story["id"], "title": title, "reason": story["reason"]}
    if title_language:
        entry["title_language"] = title_language
    if title != story.get("title") and title == (story.get("headlines") or {}).get(str(language).lower()):
        # Shown beside the heading, so a listener can tell a translated headline from a publisher's own.
        entry["source_title"] = story.get("title", "")
    return entry


def generate_briefing(store, profile, *, listener_id=None, region="all", voice_preset="default",
                      spoken_fact_ids=None, feedback_events=None, briefing_id=None,
                      available_at=None, persist=True, scheduled=False):
    profile = deepcopy(profile)
    profile["region"] = region
    ranking, summaries, _, has_stories = store.recommendation_snapshot(_build_recommendations)
    if not has_stories:
        raise ValueError("No prepared news yet. Run the import command for this mode and corpus")
    limit = max(1, int(profile.get("duration", 5) or 5))
    # Narration stops at the requested story count. Stories with nothing that can be spoken faithfully
    # are passed over, so the next ranked story takes their place.
    ranked = pipeline.rank(ranking, profile, feedback_events=feedback_events, spoken_fact_ids=spoken_fact_ids, region=region)
    if spoken_fact_ids:
        # Stories this listener has not heard come first; one they have heard returns, with its remaining
        # figures, only after those. Tomorrow's briefing is new stories, not leftovers of today's.
        heard_facts = set(spoken_fact_ids)
        heard_stories = {story["id"] for story in ranking if any(fact["id"] in heard_facts for fact in story.get("facts", []))}
        ranked = sorted(ranked, key=lambda story: story["id"] in heard_stories)
    # Far more candidates than a briefing plays, so stories with nothing speakable can be passed over.
    selected = pipeline.diversify(ranked, limit=limit * 8 + 24)
    if not selected:
        raise NoRelevantStories(
            "You are caught up: you have heard every matching story from the last seven days" if spoken_fact_ids
            else "No sourced stories match this profile")
    if spoken_fact_ids:
        # Remembered facts change what can be said, so narrate this listener's own script. A remembered figure is
        # never replayed as news; where it gives context to an update of the same measure it is recalled briefly.
        # Narration is deterministic (no model rearranges it): listeners in the same state get identical text, so
        # their story clips are rendered once and shared, and no per-listener model call is made.
        first_heard = store.first_spoken_times(listener_id, spoken_fact_ids) if listener_id else {}
        heard = pipeline.heard_figures(ranking, spoken_fact_ids, first_heard)
        segments = narrate(selected, profile, None, heard)
    else:
        segments = select_summaries(selected, profile, summaries)
    segments = pipeline.introduce_once(segments)
    if not segments:
        raise ValueError("No narration passed the evidence checks")
    used = {fact for segment in segments for fact in segment["fact_ids"]}
    spoken_words = sum(len(f"{segment.get('refresher_text', '')} {segment['text']}".split()) for segment in segments)
    briefing = dict(
        id=briefing_id or uuid4().hex,
        listener_id=listener_id,
        scheduled=scheduled,
        available_at=available_at,
        profile_name=profile["name"],
        profile_id=profile.get("id"),
        language=profile["language"],
        region=region,
        voice_preset=voice_preset,
        stories=[
            _briefing_story(story, profile["language"])
            for story in selected if any(fact["id"] in used for fact in story["facts"])
        ],
        segments=segments,
        requested_minutes=limit,
        estimated_seconds=round(spoken_words * 60 / SPOKEN_WORDS_PER_MINUTE),
        audio_url=None,
        audio_status="pending",
    )
    if spoken_fact_ids:
        # What the listener's memory did to this briefing, for the "Heard before" badges in the transcript:
        # how many stories they have already heard, and which of the stories played here continue one of those.
        briefing["memory"] = {
            "heard_stories": len(heard_stories),
            "heard_story_ids": [story["id"] for story in briefing["stories"] if story["id"] in heard_stories],
        }
    if persist:
        store.save_briefing(briefing)
    return render_audio(store, briefing, save=persist)


def _listener_audio_url(briefing):
    url = briefing.get("audio_url")
    if url and briefing.get("listener_id"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query["listener_id"] = briefing["listener_id"]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url


def render_audio(store, briefing, providers=None, *, save=True):
    """Attach narration to a saved briefing; failures keep the transcript and stay retryable."""
    stored_url = str(briefing.get("audio_url") or "").split("?", 1)[0] or None
    if audio_file(store, stored_url):
        briefing["audio_url"] = _listener_audio_url(dict(briefing, audio_url=stored_url))
        if briefing.get("audio_status") == "ready":
            if save:
                _record_briefing_facts(store, briefing)
            return briefing
        briefing["audio_status"] = "ready"
    else:
        # A missing file (for example after cleanup) is re-rendered instead of returning a dead URL.
        briefing["audio_url"] = None
        try:
            configured = providers or Providers(MODE, store.record_usage, briefing.get("voice_preset", "default"))
            briefing["audio_url"] = _listener_audio_url(
                dict(briefing, audio_url=create_audio(briefing, store, configured, MODE)))
            briefing["audio_status"] = "ready"
        except Exception as exc:
            logging.getLogger(__name__).warning("Audio generation failed (%s)", type(exc).__name__)
            briefing["audio_status"] = "unavailable"
    if not save:
        return briefing
    try:
        saved = store.save_briefing_audio(briefing)
    except Exception as exc:
        # The transcript was already saved; return it so the listener can read it and retry audio.
        logging.getLogger(__name__).warning("Could not save audio status (%s)", type(exc).__name__)
        return briefing
    _record_briefing_facts(store, briefing)
    return saved


def _record_briefing_facts(store, briefing):
    if briefing.get("listener_id") and briefing.get("audio_status") == "ready":
        fact_ids = {fact_id for segment in briefing.get("segments", []) for fact_id in segment.get("fact_ids", [])}
        spoken_at = briefing.get("available_at") or iso_utc(utc_now())
        store.record_spoken_facts(briefing["listener_id"], fact_ids, briefing["id"], spoken_at)


RATE_PREFIX = {"ingestion": "OPENAI_EMBEDDING", "story_extraction": "OPENAI", "script_generation": "OPENAI"}


def usage_report(store, mode=None):
    """Preparation (ingestion + extraction + label translation) is reported apart from the average cost of one briefing.

    Preparation covers the corpus currently loaded. Per briefing = script spend averaged over saved
    briefings + speech spend averaged over briefings with successful audio, including cache hits.
    When there is no playable briefing yet, any speech spend is spread over saved briefings.
    Live USD figures use the rates configured when the report is built.
    """
    mode = mode or MODE
    usage, article_count, story_count, briefing_count, narrated_count = store.cost_snapshot(mode)
    rows = {row["stage"]: row for row in usage}
    stages = []
    for stage in STAGES:
        row = dict(rows.get(stage) or dict(stage=stage, calls=0, input_tokens=0, output_tokens=0, characters=0))
        if mode == "demo":
            row["estimated_usd"] = 0.0
        elif row["calls"]:
            row["estimated_usd"] = Providers._usd(
                row["input_tokens"], row["output_tokens"], row["characters"], prefix=RATE_PREFIX.get(stage, "OPENAI"),
            )
            if stage == "script_generation":
                # Narration may use a pricier model; prefer its own rates, as recording does.
                script = Providers._usd(row["input_tokens"], row["output_tokens"], prefix="OPENAI_SCRIPT")
                row["estimated_usd"] = script if script is not None else row["estimated_usd"]
        else:
            row["estimated_usd"] = 0.0
        stages.append(row)
    by_stage = {row["stage"]: row for row in stages}

    def total(names):
        values = [by_stage[name]["estimated_usd"] for name in names]
        return None if None in values else sum(values)

    def average(name, count):
        value = by_stage[name]["estimated_usd"]
        return None if value is None else (value / count if count else 0.0)

    prep_cost = total(PREPARATION_STAGES)
    per_briefing = None
    if briefing_count:
        script = average("script_generation", briefing_count)
        speech = average("speech_generation", narrated_count or briefing_count)
        if script is not None and speech is not None:
            per_briefing = script + speech
    return dict(
        mode=mode,
        article_count=article_count,
        story_count=story_count,
        stages=stages,
        preparation_usd=prep_cost,
        per_briefing_usd=per_briefing,
        ten_x_preparation_usd=10 * prep_cost if prep_cost is not None else None,
    )
