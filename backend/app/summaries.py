"""Shared, evidence-backed summary variants for a prepared corpus."""

import logging
from copy import deepcopy

from .config import STYLES
from .pipeline import (
    _TOPIC_PATTERNS, _mentions_topic, _positive_topic_weights, _topic_key, _trust_eligible, narrate, reads_plain_figures,
)

_LANGUAGES = ("en", "de", "nl")
_NEUTRAL = ""
_MAX_ANGLES = 3
_LOG = logging.getLogger(__name__)


def _story_summary(story):
    """Every variant of one story, or None when nothing in it can be spoken faithfully.

    A figure is named in the language of the article that reports it, so the figures a story speaks, and
    with them its cited facts and sources, can differ between languages. Each language therefore keeps
    its own citations; a language in which nothing can be said has no variants and is passed over.
    """
    topics = set(_TOPIC_PATTERNS) | {_topic_key(topic) for topic in story.get("topics", [])}
    topics.discard("")
    evidence = [fact["evidence"] for fact in story["facts"]]
    coverage = {topic: sum(_mentions_topic(topic, excerpt) for excerpt in evidence) for topic in topics}
    angles = sorted(
        (topic for topic in topics if coverage[topic]),
        key=lambda topic: (-_mentions_topic(topic, story["title"]), -coverage[topic], topic),
    )[:_MAX_ANGLES]
    variants = {angle: {} for angle in [_NEUTRAL, *angles]}
    segments = {}
    for language in _LANGUAGES:
        rendered = {}
        for angle in variants:
            for expertise in STYLES:
                result = narrate([story], {
                    "language": language, "expertise": expertise,
                    "topic_weights": {angle: 1} if angle else {},
                }, None)
                if not result:
                    rendered = None
                    break
                rendered[(angle, expertise)] = result[0]
            if rendered is None:
                break
        if not rendered:
            continue
        for (angle, expertise), segment in rendered.items():
            variants[angle][language + "/" + expertise] = segment["text"]
        segments[language] = {key: value for key, value in next(iter(rendered.values())).items() if key != "text"}
    if not segments:
        return None
    # "segment" is the citation set of the first language that can speak the story, kept for readers of
    # older corpora; "segments" holds each language's own.
    return {"segment": next(iter(segments.values())), "segments": segments, "variants": variants}


def prepare_summaries(stories):
    """Prepare a fixed number of variants per story and retain each language's provenance."""
    summaries = {}
    for story in stories:
        eligible, _ = _trust_eligible(story)
        if not eligible:
            continue
        try:
            summary = _story_summary(story)
        except Exception as exc:  # one malformed story must not discard a whole paid import
            _LOG.warning("Story %s could not be summarized (%s); it will not be spoken", story.get("id"), type(exc).__name__)
            continue
        if summary:
            summaries[story["id"]] = summary
    return summaries


def select_summaries(ranked, profile, summaries):
    """Select a shared angle by the dot product with its single-topic vector."""
    weights = {}
    for topic, weight in _positive_topic_weights(profile).items():
        key = _topic_key(topic)
        weights[key] = weights.get(key, 0.0) + weight
    version = profile["language"] + "/" + profile.get("expertise", "intermediate")
    duration = max(1, int(profile.get("duration", 5) or 5))
    entries = []
    for story in ranked:
        summary = summaries.get(story["id"])
        if not summary:
            continue
        variants = summary["variants"]
        # Neutral wins a zero-score tie. Positive ties use the prepared angle order.
        angle = max(variants, key=lambda candidate: weights.get(candidate, 0.0))
        text = variants[angle].get(version)
        if not text:
            continue    # nothing in this story can be said in the listener's language
        citations = summary.get("segments", {}).get(profile["language"]) or summary["segment"]
        entries.append(({**deepcopy(citations), "text": text}, reads_plain_figures(text)))
        if sum(not plain for _, plain in entries) >= duration:
            break
    # Stories whose figures are named come first; one read as plain figures fills a place only when there
    # are not enough of those. Ranking order is kept.
    spare = max(0, duration - sum(not plain for _, plain in entries))
    segments = []
    for segment, plain in entries:
        if plain and spare <= 0:
            continue
        spare -= plain
        segments.append(segment)
        if len(segments) >= duration:
            break
    return segments
