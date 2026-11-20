"""Shared, evidence-backed summary variants for a prepared corpus."""

from copy import deepcopy

from .config import STYLES
from .pipeline import (
    _TOPIC_PATTERNS, _mentions_topic, _positive_topic_weights, _topic_key, _trust_eligible, narrate,
)

_LANGUAGES = ("en", "de", "nl")
_NEUTRAL = ""
_MAX_ANGLES = 3


def prepare_summaries(stories):
    """Prepare a fixed number of variants per story and retain provenance once."""
    summaries = {}
    for story in stories:
        eligible, _ = _trust_eligible(story)
        if not eligible:
            continue
        topics = set(_TOPIC_PATTERNS) | {_topic_key(topic) for topic in story.get("topics", [])}
        topics.discard("")
        evidence = [fact["evidence"] for fact in story["facts"]]
        coverage = {topic: sum(_mentions_topic(topic, excerpt) for excerpt in evidence) for topic in topics}
        angles = sorted(
            (topic for topic in topics if coverage[topic]),
            key=lambda topic: (-_mentions_topic(topic, story["title"]), -coverage[topic], topic),
        )[:_MAX_ANGLES]

        variants = {}
        for angle in [_NEUTRAL, *angles]:
            versions = {}
            for language in _LANGUAGES:
                for expertise in STYLES:
                    segment, = narrate([story], {
                        "language": language, "expertise": expertise,
                        "topic_weights": {angle: 1} if angle else {},
                    }, None)
                    versions[language + "/" + expertise] = segment["text"]
            variants[angle] = versions
        summaries[story["id"]] = {
            "segment": {key: value for key, value in segment.items() if key != "text"},
            "variants": variants,
        }
    return summaries


def select_summaries(ranked, profile, summaries):
    """Select a shared angle by the dot product with its single-topic vector."""
    weights = {}
    for topic, weight in _positive_topic_weights(profile).items():
        key = _topic_key(topic)
        weights[key] = weights.get(key, 0.0) + weight
    version = profile["language"] + "/" + profile.get("expertise", "intermediate")
    duration = max(1, int(profile.get("duration", 5) or 5))
    segments = []
    for story in ranked:
        if len(segments) >= duration:
            break
        summary = summaries.get(story["id"])
        if not summary:
            continue
        variants = summary["variants"]
        # Neutral wins a zero-score tie. Positive ties use the prepared angle order.
        angle = max(variants, key=lambda candidate: weights.get(candidate, 0.0))
        segments.append({**deepcopy(summary["segment"]), "text": variants[angle][version]})
    return segments
