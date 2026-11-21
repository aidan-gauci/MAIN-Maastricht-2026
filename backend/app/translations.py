"""Headlines and measure names in the listeners' other languages, prepared once per story.

A figure is named in the words of the article that reports it, and a story is announced by its lead
article's headline. A listener in another language would hear neither: only bare figures. One small-model
request per story translates those short labels. Figures never pass through it: values, units and evidence
stay exactly what the article printed.

Every translation is checked before it is kept. It must repeat the numbers of the original and add none,
a measure name must hold no number and no advice word, and its length must be in proportion. A label that
fails is left out, and that story is narrated in that language as it would be without the translation.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import supported_languages
from .pipeline import _ADVICE_HEADLINE, _ADVICE_WORDS, _trust_eligible

_LOG = logging.getLogger(__name__)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_MAX_MEASURE_WORDS = 8
# Dutch "verkopen" is also the plural noun "sales" ("verkopen in het tweede kwartaal"), so in a translated
# measure name it is not an advice word; the original name already passed the advice check.
_ADVICE_IN_LABEL = re.compile(r"\b(?:verkopen)\b", re.I)


def digits(text: str) -> list[str]:
    """The numbers in a text, without separators, so "4.0%" and "4,0 %" compare equal."""
    return sorted(re.sub(r"[.,]", "", number) for number in _NUMBER.findall(str(text)))


def title_language(story: dict[str, Any]) -> str:
    """The language of the story's title: that of the member article it was taken from."""
    title = " ".join(str(story.get("title", "")).split())
    articles = story.get("articles", [])
    for article in articles:
        if " ".join(str(article.get("title") or "").split()) == title and article.get("language"):
            return str(article["language"]).lower()
    languages = [str(article.get("language") or "en").lower() for article in articles]
    return "en" if "en" in languages or not languages else languages[0]


def valid_headline(original: str, translation: Any) -> str:
    text = " ".join(str(translation or "").split()) if isinstance(translation, str) else ""
    if not text or digits(text) != digits(original) or len(text) > 3 * len(original) + 20 or _ADVICE_HEADLINE.search(text):
        return ""
    return text


def valid_measure(original: str, translation: Any) -> str:
    text = " ".join(str(translation or "").split()).rstrip(".") if isinstance(translation, str) else ""
    if (not text or _NUMBER.search(text) or _ADVICE_WORDS.search(_ADVICE_IN_LABEL.sub("", text))
            or len(text.split()) > _MAX_MEASURE_WORDS
            or len(text) > 3 * len(original) + 20):
        return ""
    return text


def _wanted(story: dict[str, Any], languages: tuple[str, ...]) -> dict[str, list[tuple[str, Any, tuple[str, ...]]]]:
    """Per source language, the labels of a story still to translate: (text, where it goes, target languages)."""
    by_id = {str(article.get("article_id")): article for article in story.get("articles", [])}
    native = {str(article.get("language") or "").lower() for article in story.get("articles", []) if article.get("title")}
    wanted: dict[str, list[tuple[str, Any, tuple[str, ...]]]] = {}
    source = title_language(story)
    done = story.get("headlines") or {}
    # A language with an edition of its own already has a headline written by the publisher.
    targets = tuple(language for language in languages
                    if language != source and language not in done and (language == "en" or language not in native))
    title = " ".join(str(story.get("title", "")).split())
    if title and targets:
        wanted.setdefault(source, []).append((title, story, targets))
    for fact in story.get("facts", []):
        measure = str(fact.get("measure") or "").strip()
        if not measure:
            continue
        source = str(fact.get("language") or by_id.get(str(fact.get("article_id")), {}).get("language") or "en").lower()
        done = fact.get("measure_translations") or {}
        targets = tuple(language for language in languages if language != source and language not in done)
        if targets:
            wanted.setdefault(source, []).append((measure, fact, targets))
    return wanted


def translate_story(story: dict[str, Any], providers: Any, languages: tuple[str, ...]) -> int:
    """Translate one story's headline and measure names in place; returns how many labels were kept."""
    kept = 0
    for source, items in _wanted(story, languages).items():
        targets = tuple(dict.fromkeys(language for *_, wanted in items for language in wanted))
        texts = list(dict.fromkeys(text for text, *_ in items))
        reply = providers.translate_labels(texts, source, targets)
        if not isinstance(reply, dict):
            continue
        for language in targets:
            translated = reply.get(language)
            if not isinstance(translated, list) or len(translated) != len(texts):
                continue    # a reply that does not answer text for text cannot be matched up
            by_text = dict(zip(texts, translated))
            for text, owner, wanted in items:
                if language not in wanted:
                    continue
                if owner is story:
                    value = valid_headline(text, by_text[text])
                    if value:
                        story.setdefault("headlines", {})[language] = value
                else:
                    value = valid_measure(text, by_text[text])
                    if value:
                        owner.setdefault("measure_translations", {})[language] = value
                kept += bool(value)
    return kept


def translate_corpus(stories: list[dict[str, Any]], providers: Any, languages: tuple[str, ...] | None = None) -> dict[str, int]:
    """Translate the labels of every story that can be spoken. One failed story never stops the rest.

    It does nothing without a live provider or with a single listener language, and it only asks for what
    is still missing, so it can be run again after an interruption.
    """
    languages = tuple(languages or supported_languages())
    report = {"stories": 0, "labels": 0, "failed_stories": 0}
    if not getattr(providers, "live", False) or len(languages) < 2:
        return report
    todo = [story for story in stories if _trust_eligible(story)[0] and _wanted(story, languages)]

    def one(story: dict[str, Any]) -> int | None:
        try:
            return translate_story(story, providers, languages)
        except Exception as exc:
            _LOG.warning("Labels of story %s were not translated (%s)", story.get("id"), type(exc).__name__)
            return None

    workers = min(len(todo), max(1, int(os.getenv("BRIEFING_LIVE_CONCURRENCY", "8"))))
    if workers <= 1:
        results = [one(story) for story in todo]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, todo))
    for result in results:
        report["stories"] += result is not None
        report["failed_stories"] += result is None
        report["labels"] += result or 0
    return report
