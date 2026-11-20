import logging
import inspect
import os
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

from .audio import create_audio
from .config import CORPUS, DATA, MODE, STYLES, EUROPE_COUNTRIES
from . import pipeline
from .pipeline import narrate, prepare
from .providers import Providers
from .storage import Store

STAGES = ("ingestion", "story_extraction", "script_generation", "speech_generation")
UTC = timezone.utc


class NoRelevantStories(ValueError):
    pass


def utc_now():
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def next_occurrence(schedule: dict, after: datetime | None = None) -> dict[str, str]:
    """Return the next delivery whose 30-minute generation window is still ahead."""
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
        if delivery - timedelta(minutes=30) > now:
            return {"generation_at": iso_utc(delivery - timedelta(minutes=30)), "delivery_at": iso_utc(delivery)}
        day += timedelta(days=1)
    raise ValueError("Could not find a future schedule occurrence")


def schedule_envelope(schedule: dict) -> dict[str, str | int]:
    return {"schedule_id": schedule["schedule_id"], "schedule_version": schedule["schedule_version"],
            "generation_at": schedule["generation_at"], "delivery_at": schedule["delivery_at"]}


def scheduled_run_id(envelope: dict) -> str:
    return uuid5(NAMESPACE_URL, "liquid-briefing:" + ":".join(str(envelope[key]) for key in ("schedule_id", "schedule_version", "generation_at"))).hex


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


def get_providers(store):
    return Providers(MODE, store.record_usage)


def prepare_corpus(store, articles):
    if not articles or len({row["article_id"] for row in articles}) != len(articles):
        raise ValueError("Import requires nonempty input with unique article IDs")
    stories = prepare(articles, get_providers(store))
    if not stories:
        raise ValueError("Preparation produced no stories")
    by_id = {article["article_id"]: article for article in articles}
    for story in stories:
        for source in story.get("articles", []):
            article = by_id.get(source.get("article_id"), {})
            for key in ("region", "country"):
                if article.get(key):
                    source[key] = article[key]
    store.replace_corpus(articles, stories)
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


def generate_briefing(store, profile, *, listener_id=None, region="all", voice_preset="default",
                      spoken_fact_ids=None, feedback_events=None, briefing_id=None,
                      available_at=None, persist=True, scheduled=False):
    profile = deepcopy(profile)
    profile["region"] = region
    providers = Providers(MODE, store.record_usage, voice_preset)
    selected = rank_for_listener(store.stories(), profile, feedback_events=feedback_events,
                                 spoken_fact_ids=spoken_fact_ids, region=region, scheduled=scheduled)
    if not selected:
        raise NoRelevantStories("No sourced stories match this profile")
    segments = narrate(selected, profile, providers)
    if not segments:
        raise ValueError("No narration passed the evidence checks")
    used = {fact for segment in segments for fact in segment["fact_ids"]}
    briefing = dict(
        id=briefing_id or uuid4().hex,
        listener_id=listener_id,
        scheduled=scheduled,
        available_at=available_at,
        profile_name=profile["name"],
        profile_id=profile["id"],
        language=profile["language"],
        region=region,
        voice_preset=voice_preset,
        stories=[
            {key: story[key] for key in ("id", "title", "reason")}
            for story in selected if any(fact["id"] in used for fact in story["facts"])
        ],
        segments=segments,
        audio_url=None,
        audio_status="pending",
    )
    if persist:
        store.save_briefing(briefing)
    return render_audio(store, briefing, providers, save=persist)


def render_audio(store, briefing, providers=None, *, save=True):
    if briefing.get("audio_url"):
        if briefing.get("listener_id"):
            parts = urlsplit(briefing["audio_url"])
            query = dict(parse_qsl(parts.query))
            query["listener_id"] = briefing["listener_id"]
            briefing["audio_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        if save:
            store.save_briefing(briefing)
            _record_briefing_facts(store, briefing)
        return briefing
    try:
        configured = providers or Providers(MODE, store.record_usage, briefing.get("voice_preset", "default"))
        briefing["audio_url"] = create_audio(briefing, store, configured, MODE)
        if briefing.get("listener_id"):
            parts = urlsplit(briefing["audio_url"])
            query = dict(parse_qsl(parts.query))
            query["listener_id"] = briefing["listener_id"]
            briefing["audio_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        briefing["audio_status"] = "ready"
    except Exception as exc:
        logging.getLogger(__name__).warning("Audio generation failed (%s)", type(exc).__name__)
        briefing["audio_status"] = "unavailable"
    if save:
        store.save_briefing(briefing)
        _record_briefing_facts(store, briefing)
    return briefing


def _record_briefing_facts(store, briefing):
    if briefing.get("listener_id") and briefing.get("audio_status") == "ready":
        fact_ids = {fact_id for segment in briefing.get("segments", []) for fact_id in segment.get("fact_ids", [])}
        spoken_at = briefing.get("available_at") or iso_utc(utc_now())
        store.record_spoken_facts(briefing["listener_id"], fact_ids, briefing["id"], spoken_at)


def usage_report(store):
    rows = {row["stage"]: row for row in store.usage(MODE)}
    stages = [
        rows.get(stage) or dict(stage=stage, calls=0, input_tokens=0, output_tokens=0, characters=0, estimated_usd=None)
        for stage in STAGES
    ]

    def cost(group):
        active = [row["estimated_usd"] for row in group if row["calls"]]
        return sum(active) if active and all(value is not None for value in active) else None

    prep_cost = cost(stages[:2])
    generation_cost = cost(stages[2:]) if stages[3]["calls"] else None
    briefing_count = store.briefing_count()
    article_count = len(store.articles())
    if MODE == "demo" and article_count:
        prep_cost = 0
    return dict(
        mode=MODE,
        article_count=article_count,
        story_count=len(store.stories()),
        stages=stages,
        preparation_usd=prep_cost,
        per_briefing_usd=generation_cost / briefing_count if generation_cost is not None and briefing_count else None,
        ten_x_preparation_usd=10 * prep_cost if prep_cost is not None else None,
    )
