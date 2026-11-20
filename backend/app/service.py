import logging
from collections import Counter
from uuid import uuid4

from .audio import audio_file, create_audio
from .config import CORPUS, DATA, MODE, STYLES
from .pipeline import narrate, prepare, rank
from .providers import Providers
from .storage import Store

STAGES = ("ingestion", "story_extraction", "script_generation", "speech_generation")


def get_store():
    directory = DATA / CORPUS / MODE
    return Store(directory / "briefings.sqlite3", directory / "audio")


def get_providers(store):
    return Providers(MODE, store.record_usage)


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
    # Usage rows up to this mark belong to earlier imports and are replaced with the corpus.
    previous_usage = store.usage_watermark()
    providers = get_providers(store)
    stories = prepare(articles, providers)
    if getattr(providers, "extraction_failed", False):
        if not allow_fallback:
            raise ValueError("Live extraction failed; the previous corpus was kept. Retry, or pass --allow-offline-fallback")
        logging.getLogger(__name__).warning("Live extraction failed; stored the offline heuristic clusters")
    if not stories:
        raise ValueError("Preparation produced no stories")
    store.replace_corpus(articles, stories, clear_preparation_usage_through=previous_usage)
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


def generate_briefing(store, profile):
    providers = get_providers(store)
    stories = store.stories()
    if not stories:
        raise ValueError("No prepared news yet. Run the import command for this mode and corpus")
    selected = rank(stories, profile)
    if not selected:
        raise ValueError("No sourced stories match this profile")
    segments = narrate(selected, profile, providers)
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
    return render_audio(store, briefing, providers)


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
        store.save_briefing(briefing)
    except Exception as exc:
        # The transcript was already saved; return it so the listener can read it and retry audio.
        logging.getLogger(__name__).warning("Could not save audio status (%s)", type(exc).__name__)
    return briefing


RATE_PREFIX = {"ingestion": "OPENAI_EMBEDDING", "story_extraction": "OPENAI", "script_generation": "OPENAI"}


def usage_report(store, mode=None):
    """Preparation (ingestion + extraction) is reported apart from the average cost of one briefing.

    Preparation covers the corpus currently loaded. Per briefing = script spend averaged over saved
    briefings + speech spend averaged over successful renders, so failed audio does not dilute it.
    Live USD figures use the rates configured when the report is built.
    """
    mode = mode or MODE
    rows = {row["stage"]: row for row in store.usage(mode)}
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
    briefing_count = store.briefing_count()
    article_count = len(store.articles())

    def total(names):
        values = [by_stage[name]["estimated_usd"] for name in names]
        return None if None in values else sum(values)

    def average(name, count):
        value = by_stage[name]["estimated_usd"]
        return None if value is None else (value / count if count else 0.0)

    prep_cost = total(("ingestion", "story_extraction"))
    per_briefing = None
    if briefing_count:
        script, speech = average("script_generation", briefing_count), average("speech_generation", by_stage["speech_generation"]["calls"])
        if script is not None and speech is not None:
            per_briefing = script + speech
    return dict(
        mode=mode,
        article_count=article_count,
        story_count=len(store.stories()),
        stages=stages,
        preparation_usd=prep_cost,
        per_briefing_usd=per_briefing,
        ten_x_preparation_usd=10 * prep_cost if prep_cost is not None else None,
    )
