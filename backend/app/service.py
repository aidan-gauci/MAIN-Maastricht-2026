import logging
from uuid import uuid4

from .audio import create_audio
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


def prepare_corpus(store, articles):
    if not articles or len({row["article_id"] for row in articles}) != len(articles):
        raise ValueError("Import requires nonempty input with unique article IDs")
    stories = prepare(articles, get_providers(store))
    if not stories:
        raise ValueError("Preparation produced no stories")
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


def generate_briefing(store, profile):
    providers = get_providers(store)
    selected = rank(store.stories(), profile)
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
    if briefing.get("audio_url"):
        return briefing
    try:
        briefing["audio_url"] = create_audio(briefing, store, providers or get_providers(store), MODE)
        briefing["audio_status"] = "ready"
    except Exception as exc:
        logging.getLogger(__name__).warning("Audio generation failed (%s)", type(exc).__name__)
        briefing["audio_status"] = "unavailable"
    store.save_briefing(briefing)
    return briefing


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
