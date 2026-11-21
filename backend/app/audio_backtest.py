"""Backtest of audio generation: how much speech a simulated audience would have rendered and billed.

It runs the real briefing code on the imported corpus for a simulated audience over several days and
plans every briefing's clips with the same function the app uses, without synthesizing anything, so it
makes no provider call. For each audience size it reports the clips requested, the clips actually
rendered (a clip is rendered once per text, voice and language, then reused), the characters billed, and
the cost per listener. It also checks the promises the audio makes: no fact is replayed as news to the
same listener, and every spoken fact cites an article of its own story.
"""
from __future__ import annotations

import random
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from . import service
from .audio import clip_plan
from .config import STYLES, VOICE_CATALOGUE, load_profiles
from .providers import with_pauses

DAYS_PER_MONTH = 30
LANGUAGE_SHARES = {"en": 0.70, "de": 0.15, "nl": 0.15}
DEFAULT_VOICE_SHARE = 0.5          # the rest is spread evenly over the other voices
SAVED_PROFILE_SHARE = 0.7          # the rest build a custom briefing from one or two topics
CUSTOM_TOPICS = 8                  # custom briefings draw from the corpus's most common topics


def _population(count, profiles, topics, voices, rng, even):
    """Listener settings. ``even`` spreads choices evenly over the options: the worst case for sharing."""
    languages = list(LANGUAGE_SHARES)
    language_weights = None if even else list(LANGUAGE_SHARES.values())
    voice_weights = None if even else [DEFAULT_VOICE_SHARE] + [(1 - DEFAULT_VOICE_SHARE) / (len(voices) - 1)] * (len(voices) - 1)
    listeners = []
    for _ in range(count):
        language = rng.choices(languages, language_weights)[0]
        voice = rng.choices(voices, voice_weights)[0]
        if rng.random() < SAVED_PROFILE_SHARE or not topics:
            base = rng.choice(profiles)
            profile = dict(base, language=language)
            key = ("saved", base["id"], language)
        else:
            chosen = tuple(sorted(rng.sample(topics, rng.choice((1, 2)) if len(topics) > 1 else 1)))
            expertise, duration = rng.choice(list(STYLES)), rng.choice((3, 5, 8))
            profile = dict(id="wildcard", name="Custom", topic_weights=dict.fromkeys(chosen, 1.0), excluded_topics=[],
                           expertise=expertise, language=language, duration=duration, style=STYLES[expertise])
            key = ("custom", chosen, expertise, duration, language)
        listeners.append({"profile": profile, "key": key, "voice": voice, "heard": frozenset(), "stories": frozenset()})
    return listeners


def _chain(database, audio_dir, profile, days):
    """One listener type's briefings on consecutive days, each remembering what the earlier ones said."""
    from .storage import Store
    store = Store(database, audio_dir)
    heard: set[str] = set()
    chain = []
    # Planning only: the stub replaces the step that would synthesize speech.
    with patch.object(service, "create_audio", return_value="/api/audio/backtest.mp3"):
        for _ in range(days):
            try:
                briefing = service.generate_briefing(store, profile, spoken_fact_ids=sorted(heard), persist=False)
            except ValueError:
                chain.append(None)    # nothing left that this listener has not heard
                continue
            chain.append({key: briefing[key] for key in ("language", "stories", "segments")})
            heard |= {fact for segment in briefing["segments"] for fact in segment["fact_ids"]}
    return chain


def run(store, audiences=(100, 1000, 10000), days=3, seed=1, even=False, usd_per_1000_chars=0.08, workers=1, computed=None):
    """Simulate each audience size from a cold cache and return one report row per size.

    ``computed`` may carry listener types worked out by an earlier run on the same corpus and days.
    """
    stories = store.stories()
    if not stories:
        raise ValueError("No prepared news; import a corpus first")
    fact_articles = {fact["id"]: fact["article_id"] for story in stories for fact in story["facts"]}
    story_articles = {story["id"]: {article["article_id"] for article in story["articles"]} for story in stories}
    profiles = [profile.model_dump() for profile in load_profiles()]
    topics = [topic for topic, _ in Counter(topic for story in stories for topic in story.get("topics", [])
                                            if topic != "news").most_common(CUSTOM_TOPICS)]
    voices = ["default", *(row["id"] for row in VOICE_CATALOGUE)]
    populations = {count: _population(count, profiles, topics, voices, random.Random(seed), even) for count in audiences}
    # Listeners with the same settings hear the same briefings (narration is deterministic), so each
    # distinct listener type is computed once, in parallel when there are many.
    types = {listener["key"]: listener["profile"] for listeners in populations.values() for listener in listeners}
    chains = computed if computed is not None else {}
    missing = [key for key in types if key not in chains]
    arguments = [(str(store.path), str(store.audio_dir), types[key], days) for key in missing]
    if workers > 1 and len(missing) > workers:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            chains.update(zip(missing, pool.map(_chain, *zip(*arguments), chunksize=4)))
    else:
        chains.update((key, _chain(*argument)) for key, argument in zip(missing, arguments))
    reports = []
    plans: dict = {}
    for count, listeners in populations.items():
        rendered: set[str] = set()
        totals = Counter()
        by_kind = Counter()
        daily = []
        for day in range(1, days + 1):
            day_chars = 0
            for listener in listeners:
                briefing = chains[listener["key"]][day - 1]
                if briefing is None:
                    totals["briefings_without_news"] += 1
                    continue
                totals["briefings"] += 1
                plan_key = (listener["key"], day, listener["voice"])
                if plan_key not in plans:
                    provider = SimpleNamespace(voice_id=lambda voice=listener["voice"]: voice)
                    plans[plan_key] = [(kind, len(with_pauses(text)), key) for kind, text, key in clip_plan(briefing, provider, "live")]
                for kind, billed, key in plans[plan_key]:
                    totals["clips_requested"] += 1
                    totals["chars_requested"] += billed
                    if key not in rendered:
                        rendered.add(key)
                        totals["clips_rendered"] += 1
                        totals["chars_billed"] += billed
                        by_kind[kind] += billed
                        day_chars += billed
                # Promises: nothing heard before is news again, and every spoken fact cites its own story.
                spoken, story_ids = set(), {story["id"] for story in briefing["stories"]}
                own_articles = set().union(*(story_articles.get(story_id, set()) for story_id in story_ids))
                for segment in briefing["segments"]:
                    recalled = set(segment.get("refresher_fact_ids") or [])
                    news = set(segment["fact_ids"]) - recalled
                    totals["facts_spoken"] += len(news)
                    totals["facts_replayed"] += len(news & listener["heard"])
                    cited = {source["article_id"] for source in segment["sources"]}
                    totals["facts_uncited"] += sum(fact_articles.get(fact) not in cited for fact in segment["fact_ids"])
                    totals["facts_outside_story"] += sum(fact_articles.get(fact) not in own_articles for fact in news)
                    totals["refreshers"] += bool(segment.get("refresher_text"))
                    spoken |= set(segment["fact_ids"])
                totals["stories_played"] += len(story_ids)
                totals["stories_returning"] += len(story_ids & listener["stories"])
                listener["heard"] = listener["heard"] | spoken
                listener["stories"] = listener["stories"] | story_ids
            daily.append(round(day_chars * usd_per_1000_chars / 1000, 2))
        cost = totals["chars_billed"] * usd_per_1000_chars / 1000
        unshared = totals["chars_requested"] * usd_per_1000_chars / 1000
        reports.append({
            "listeners": count, "days": days, "briefings": totals["briefings"],
            "briefings_without_news": totals["briefings_without_news"],
            "clips_requested": totals["clips_requested"], "clips_rendered": totals["clips_rendered"],
            "clips_reused_percent": round(100 * (1 - totals["clips_rendered"] / max(1, totals["clips_requested"])), 1),
            "characters_billed": totals["chars_billed"],
            "characters_by_clip_kind": dict(by_kind),
            "cost_usd": round(cost, 2), "cost_usd_by_day": daily,
            "cost_usd_without_sharing": round(unshared, 2),
            "cost_per_listener_per_month_usd": round(cost / count / days * DAYS_PER_MONTH, 3),
            # The corpus is fixed, so later days have fewer new stories than a live news day would.
            # Counting every day like the first (a cold cache and all-new stories) is the upper bound.
            "cost_per_listener_per_month_upper_bound_usd": round(daily[0] / count * DAYS_PER_MONTH, 3),
            "cost_per_listener_per_month_without_sharing_usd": round(unshared / count / days * DAYS_PER_MONTH, 3),
            "facts_spoken": totals["facts_spoken"], "facts_replayed_as_news": totals["facts_replayed"],
            "facts_without_their_source": totals["facts_uncited"] + totals["facts_outside_story"],
            "refresher_lines": totals["refreshers"],
            "stories_played": totals["stories_played"], "stories_returning_with_new_figures": totals["stories_returning"],
        })
    return {
        "corpus": {"stories": len(stories), "listener_types_computed": len(types)},
        "assumptions": {
            "usd_per_1000_characters": usd_per_1000_chars, "voices": len(voices), "languages": list(LANGUAGE_SHARES),
            "choices": "spread evenly (worst case)" if even else "70% English, half on the default voice, 70% on a saved profile",
            "seed": seed, "month": f"{DAYS_PER_MONTH} daily briefings, extrapolated from {days} simulated days on a fixed corpus",
        },
        "audiences": reports,
    }
