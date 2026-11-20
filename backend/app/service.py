import logging
from collections import Counter
from uuid import uuid4

from .audio import audio_file, create_audio
from .config import CORPUS, DATA, MODE, STYLES
from .pipeline import prepare, prepare_ranking, rank
from .providers import Providers
from .storage import Store
from .summaries import prepare_summaries, select_summaries

STAGES = ("ingestion", "story_extraction", "script_generation", "speech_generation")


def get_store():
    directory = DATA / CORPUS / MODE
    return Store(directory / "briefings.sqlite3", directory / "audio")


def get_providers(store, *, record_usage=None):
    return Providers(MODE, record_usage or store.record_usage)


def prepare_corpus(store, articles, *, allow_fallback=True):
    """Prepare and store a corpus; on any error the previously imported corpus is kept.

    With ``allow_fallback=False`` a failed live extraction aborts instead of silently storing
    the local heuristic clusters.
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
    store.replace_corpus(articles, stories, prepare_summaries(stories), preparation_usage=usage)
    return stories


def list_topics(stories):
    return sorted({topic for story in stories for topic in story.get("topics", [])})


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


def generate_briefing(store, profile):
    ranking, summaries, _, has_stories = store.recommendation_snapshot(_build_recommendations)
    if not has_stories:
        raise ValueError("No prepared news yet. Run the import command for this mode and corpus")
    limit = max(1, int(profile.get("duration", 5) or 5))
    selected = rank([], profile, prepared=ranking, limit=limit)
    if not selected:
        raise ValueError("No sourced stories match this profile")
    segments = select_summaries(selected, profile, summaries)
    if not segments:
        raise ValueError("No narration passed the evidence checks")
    used = {fact for segment in segments for fact in segment["fact_ids"]}
    briefing = dict(
        id=uuid4().hex,
        profile_name=profile["name"],
        language=profile["language"],
        stories=[
            {key: story[key] for key in ("id", "title", "reason")}
            for story in selected if any(fact["id"] in used for fact in story["facts"])
        ],
        segments=segments,
        audio_url=None,
        audio_status="pending",
    )
    store.save_briefing(briefing)
    return render_audio(store, briefing)


def render_audio(store, briefing, providers=None):
    """Attach narration to a saved briefing; failures keep the transcript and stay retryable."""
    if audio_file(store, briefing.get("audio_url")):
        if briefing.get("audio_status") == "ready":
            return briefing
        briefing["audio_status"] = "ready"
    else:
        # A missing file (for example after cleanup) is re-rendered instead of returning a dead URL.
        briefing["audio_url"] = None
        try:
            briefing["audio_url"] = create_audio(briefing, store, providers or get_providers(store), MODE)
            briefing["audio_status"] = "ready"
        except Exception as exc:
            logging.getLogger(__name__).warning("Audio generation failed (%s)", type(exc).__name__)
            briefing["audio_status"] = "unavailable"
    try:
        return store.save_briefing_audio(briefing)
    except Exception as exc:
        # The transcript was already saved; return it so the listener can read it and retry audio.
        logging.getLogger(__name__).warning("Could not save audio status (%s)", type(exc).__name__)
    return briefing


RATE_PREFIX = {"ingestion": "OPENAI_EMBEDDING", "story_extraction": "OPENAI", "script_generation": "OPENAI"}


def usage_report(store, mode=None):
    """Preparation (ingestion + extraction) is reported apart from the average cost of one briefing.

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

    prep_cost = total(("ingestion", "story_extraction"))
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
