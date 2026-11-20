"""Deterministic news preparation, ranking, and factual narration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from .clustering import group_articles


def fixture_articles() -> list[dict[str, Any]]:
    """Small fictional multilingual fixture for demos and offline development."""
    return [
        {"article_id":"fx-en-1","title":"Northstar reports quarterly revenue of 4.2 billion euros","source":"Fictional Wire","url":"https://example.invalid/northstar-en","publication_date":"2026-11-19","language":"en","body":"Fictional company Northstar reported revenue of 4.2 billion euros for the quarter. Its release said operating profit was 610 million euros.","official":True},
        {"article_id":"fx-de-1","title":"Northstar meldet Quartalsumsatz von 4,2 Milliarden Euro","source":"Fiktive Nachrichten","url":"https://example.invalid/northstar-de","publication_date":"2026-11-19","language":"de","body":"Das fiktive Unternehmen Northstar meldete einen Quartalsumsatz von 4,2 Milliarden Euro. Der Gewinn aus dem Betrieb betrug laut Mitteilung 590 Millionen Euro.","official":False},
        {"article_id":"fx-nl-1","title":"Northstar: kwartaalomzet 4,2 miljard euro","source":"Verzonnen Persbureau","url":"https://example.invalid/northstar-nl","publication_date":"2026-11-19","language":"nl","body":"Het fictieve bedrijf Northstar meldde een kwartaalomzet van 4,2 miljard euro. De operationele winst was volgens het bericht 610 miljoen euro.","official":False},
        {"article_id":"fx-en-2","title":"Fictional central bank holds policy rate at 3.75 percent","source":"Fictional Wire","url":"https://example.invalid/rate","publication_date":"2026-11-18","language":"en","body":"The fictional Central Bank of Exampleland kept its policy rate at 3.75 percent on Tuesday, citing unchanged inflation expectations.","official":True},
    ]


_UNIT_PATTERN = r"(?:per\s+megawatt[- ]hour|basis\s+points?|basispunkte|basispunten|million\s+crowns?|miljoen\s+kronen|millionen\s+kronen|milliarden\s+euro|miljard\s+euro|megawatt[- ]hours?|megawatts?|applications?|anträge|aanvragen|billion|million|bn|mn|percent|per\s+cent|prozent|procent|%|milliarden|millionen|miljard|miljoen|crowns?|kronen|euro|euros|eur|workers?|jobs?|hours?|days?|years?|meetings?)"
_NUM = re.compile(rf"(?<![\w])(?:€\s*)?[−-]?\d+(?:[.,]\d+)*(?:\s*{_UNIT_PATTERN})?", re.I)
_NUMBER = re.compile(r"[−-]?\d+(?:[.,]\d+)*")
_WORDS = re.compile(r"\b(?:revenue|sales|turnover|umsatz|omzet|profit|winst|gewinn|rate|policy rate|inflation|percent|per cent|billion|million|miljard|milliarden|miljoen|millionen|growth|forecast|prognose|groeiverwachting|wachstumsprognose|exports?|exporte?|export|jobs?|arbeitsplätze|banen|budget|grant|applications?|anträge|aanvragen|workers?|arbeitnehmer|werknemers|wage|pay rise|lohn|loon|prices?|preise|prijzen|prices?|energy|energie|capacity|megawatts?|receipt|receipts|nächtigungen|stays|loans?|krediet|kredite|overdue|überfällig|tourism|tourismus|toerisme|investment|investition|industrie|household|haushalt|huishouden|saving|savings|sparen|rates?|rente|leitzins|policy rate|basis points?|basispunkte|basispunten|crown|crowns|million|billion|percent|prozent|procent)\b", re.I)


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\b(?:reports?|meldet|meldete|reported|quarterly|quartals|quarter|kwartaal)\b", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _group_key(article: dict[str, Any]) -> str:
    # Locale-prefixed editions of the same story commonly share their URL
    # route. Canonicalize that route instead of merging every item from a
    # publisher on the same date, which can combine unrelated coverage.
    parsed = urlsplit(str(article.get("url", "")))
    if parsed.netloc and parsed.path:
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() in {"en", "de", "nl"}:
            parts.pop(0)
        route = "/".join(parts).casefold()
        if route:
            return f"url|{parsed.netloc.casefold()}|{route}"
    return _norm(str(article.get("title", ""))) + "|" + _day(article)


def _topic_family(context: str) -> str:
    words = [w.lower() for w in _WORDS.findall(context)]
    for family, terms in (("revenue", ("revenue", "sales", "turnover", "umsatz", "omzet")),
                          ("profit", ("profit", "winst", "gewinn")),
                          ("rate", ("rate",)), ("inflation", ("inflation",))):
        if any(w in terms for w in words): return family
    return words[0] if words else "figure"


_TOPIC_TERMS = {
    "growth": ("growth", "forecast", "wachstum", "prognose", "groei", "verwachting"),
    "trade": ("export", "trade", "handel", "exports"),
    "rates": ("rate", "rente", "leitzins", "interest", "basis point", "basispunkt"),
    "inflation": ("inflation", "prices", "preise", "prijzen", "price"),
    "jobs": ("jobs", "workers", "arbeitsplatz", "banen", "wage", "lohn", "loon", "dockworkers"),
    "energy": ("energy", "solar", "megawatt", "electricity", "energie"),
    "housing": ("housing", "rent", "home", "household", "haushalt", "huishouden"),
    "industry": ("factory", "manufactur", "industry", "industrie", "grant", "equipment"),
    "tourism": ("tourism", "tourismus", "toerisme", "visitor", "receipts", "stays", "nächtigungen"),
    "agriculture": ("farm", "agriculture", "fertilizer", "feed", "landwirtschaft"),
    "credit": ("loan", "credit", "lender", "kredit", "krediet", "payment", "overdue"),
    "transport": ("rail", "freight", "port", "harbor", "hafen", "haven", "border"),
    "markets": ("market", "bank", "auction", "investment", "exports", "trade"),
}


_TOPIC_ALIASES = {"rate": "rates", "interest rates": "rates", "trade and exports": "trade", "prices": "inflation", "employment": "jobs", "transportation": "transport"}
FEEDBACK_WEIGHT = 0.1

# ISO alpha-2 members of UN M49 region 150 (Europe).
_EUROPE_COUNTRIES = frozenset("""
    BY BG CZ HU PL MD RO RU SK UA
    AX DK EE FO FI GG IS IE IM JE LV LT NO SJ SE GB
    AL AD BA HR GI GR VA IT MT ME MK PT SM RS SI ES
    AT BE FR DE LI LU MC NL CH
""".lower().split())
_GEOGRAPHY_FIELDS = ("region", "country_code", "country")


def _topic_key(value: Any) -> str:
    text = str(value).strip().lower()
    return _TOPIC_ALIASES.get(text, text)


def _topic_terms(value: str) -> tuple[str, ...]:
    key = _topic_key(value)
    terms = _TOPIC_TERMS.get(key)
    if terms:
        return terms
    return tuple(re.findall(r"[a-z0-9]+", key))


def _matches_topic(axis: str, topics: Iterable[str]) -> bool:
    return any(axis in topic or topic in axis for topic in topics)


def _story_topics(story: dict[str, Any]) -> set[str]:
    return {_topic_key(topic) for topic in story.get("topics", [])}


def _matches_region(story: dict[str, Any], region: str) -> bool:
    if region == "all":
        return True
    geography = {
        str(record.get(field, "")).strip().lower()
        for record in (story, *story.get("articles", []))
        for field in _GEOGRAPHY_FIELDS
    }
    return region in geography or (region == "europe" and bool(geography & _EUROPE_COUNTRIES))


def _article_reference(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_id": str(article.get("article_id", "")),
        "title": article.get("title", ""),
        "source": article.get("source", ""),
        "url": article.get("url", ""),
        "language": _language(article),
        "official": bool(article.get("official", False)),
        **{field: article[field] for field in _GEOGRAPHY_FIELDS if article.get(field)},
    }


def _recency(story: dict[str, Any], reference_day: str) -> float:
    try:
        age = max(0, (date.fromisoformat(reference_day) - date.fromisoformat(str(story.get("publication_date", ""))[:10])).days)
    except ValueError:
        return 0.0
    return 1.0 / (1.0 + age)


def _novelty_values(stories: list[dict[str, Any]]) -> dict[str, float]:
    """Penalize duplicate headlines or source members in linear time."""
    title_counts = Counter(_norm(str(story.get("title", ""))) for story in stories)
    member_counts = Counter(str(article.get("article_id", "")) for story in stories for article in story.get("articles", []))
    result = {}
    for story in stories:
        duplicate_count = title_counts[_norm(str(story.get("title", "")))]
        member_duplicates = [member_counts[str(article.get("article_id", ""))] for article in story.get("articles", [])]
        if member_duplicates:
            duplicate_count = max(duplicate_count, *member_duplicates)
        result[str(story.get("id", id(story)))] = 1.0 / max(1, duplicate_count)
    return result


def _trust_eligible(story: dict[str, Any]) -> tuple[bool, str]:
    facts = story.get("facts", [])
    articles = {str(article.get("article_id", "")): article for article in story.get("articles", [])}
    if not facts:
        return False, "no sourced facts"
    sources = set()
    for fact in facts:
        article = articles.get(str(fact.get("article_id", "")))
        if not fact.get("id") or not fact.get("evidence") or not article:
            return False, "incomplete provenance"
        url = urlsplit(str(article.get("url", "")))
        if not article.get("source") or url.scheme not in {"http", "https"} or not url.netloc:
            return False, "missing publisher or source link"
        sources.add(str(article["article_id"]))
    official = any(bool(articles[article_id].get("official")) for article_id in sources)
    return True, "official source included" if official else "publisher sources cited"


def _provider_batches(groups: list[list[dict[str, Any]]]):
    """Bound each external extraction request by articles and characters."""
    article_limit = max(1, int(os.getenv("LIQUID_LIVE_BATCH_ARTICLES", "24")))
    char_limit = max(4000, int(os.getenv("LIQUID_LIVE_BATCH_CHARS", "48000")))
    article_body_limit = max(1000, int(os.getenv("LIQUID_LIVE_ARTICLE_CHARS", "10000")))
    batch: list[dict[str, Any]] = []
    batch_chars = 0
    for group in groups:
        # Keep members together when possible, then split exceptionally large
        # clusters while preserving the original member map for story links.
        for original in group:
            article = dict(original)
            article["body"] = str(article.get("body", ""))[:article_body_limit]
            estimate = len(json.dumps(article, ensure_ascii=False))
            if batch and (len(batch) >= article_limit or batch_chars + estimate > char_limit):
                yield batch
                batch, batch_chars = [], 0
            batch.append(article)
            batch_chars += estimate
    if batch:
        yield batch


def _topics(group: list[dict[str, Any]]) -> list[str]:
    blob = " ".join(str(a.get("title", "")) + " " + str(a.get("body", ""))[:2500] for a in group).lower()
    return [topic for topic, terms in _TOPIC_TERMS.items() if any(term in blob for term in terms)] or ["news"]


def _story_id(article_ids: list[str], title: str = "", day: str = "") -> str:
    """Stable cluster ID derived from its source members, not a mutable title."""
    members = sorted({str(value) for value in article_ids if str(value)})
    identity = "|".join(members) if members else day + "|" + _norm(title)
    return "story-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]


def _day(article: dict[str, Any]) -> str:
    value = str(article.get("publication_date", ""))[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return value


def _language(article: dict[str, Any]) -> str:
    return str(article.get("language", "en")).lower()


def _canonical_number(value: str, language: str = "en") -> str:
    token = _NUMBER.search(str(value))
    if not token:
        return ""
    number = token.group(0).replace("−", "-")
    if language in {"de", "nl"}:
        number = number.replace(".", "").replace(",", ".")
    else:
        number = number.replace(",", "")
    try:
        return f"{float(number):.8f}".rstrip("0").rstrip(".")
    except ValueError:
        return number


def _canonical_unit(unit: str) -> str:
    value = re.sub(r"\s+", " ", str(unit).strip().lower())
    aliases = {
        "%": "percent", "per cent": "percent", "prozent": "percent", "procent": "percent",
        "milliarden": "billion", "miljard": "billion", "millionen": "million", "miljoen": "million",
        "bn": "billion", "mn": "million", "euros": "euro", "eur": "euro",
        "crowns": "crown", "kronen": "crown", "worker": "workers", "job": "jobs",
        "megawatt hour": "megawatt-hour", "per megawatt hour": "per megawatt-hour",
        "basis point": "basis points",
        "million crowns": "million crown", "million crown": "million crown",
        "millionen kronen": "million crown", "miljoen kronen": "million crown",
        "milliarden euro": "billion euro", "miljard euro": "billion euro",
    }
    return aliases.get(value, value)


def _value_unit(value: str, unit: str = "", language: str = "en") -> tuple[str, str]:
    value_text = str(value)
    match = _NUMBER.search(value_text)
    embedded_unit = value_text[match.end():].strip() if match else ""
    return _canonical_number(value_text, language), _canonical_unit(unit or embedded_unit)


def _fact_id(article_id: str, evidence: str, value: str, unit: str = "", language: str = "en") -> str:
    canonical = "|".join(_value_unit(value, unit, language))
    return "fact-" + hashlib.sha1(f"{article_id}|{evidence}|{canonical}".encode("utf-8")).hexdigest()[:12]


def _sentence_bounds(body: str, offset: int) -> tuple[int, int]:
    starts = [match.end() for match in re.finditer(r"[.!?]\s+", body[:offset])]
    left = starts[-1] if starts else 0
    end_match = re.search(r"[.!?](?:\s+|$)", body[offset:])
    right = offset + end_match.end() if end_match else len(body)
    return left, right


def _short_evidence(body: str, offset: int, number_start: int, number_end: int, window: int = 100) -> str:
    """Keep a short exact source excerpt around the verified quantity."""
    sentence_left, sentence_right = _sentence_bounds(body, number_start)
    start = max(sentence_left, number_start - window)
    end = min(sentence_right, number_end + 60)
    if start and body[start - 1:start].isalnum():
        boundary = body.find(" ", start, number_start)
        if boundary >= 0:
            start = boundary + 1
    if end < len(body) and body[end:end + 1].isalnum():
        boundary = body.rfind(" ", number_end, end)
        if boundary >= number_end:
            end = boundary
    return body[start:end].strip()


def _sentence_hash(sentence: str) -> str:
    normalized = _norm(sentence)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _claim_terms(statement: str) -> set[str]:
    ignored = {"the", "and", "for", "from", "that", "with", "this", "was", "were", "said", "its", "of", "in", "to", "a", "an", "is", "by", "as", "at", "on", "it", "not", "according", "reported", "estimate", "estimated"}
    terms = set(re.findall(r"[a-z]+", _norm(statement))) - ignored
    return terms


def _claims_contradict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("_claim_group") == right.get("_claim_group"):
        return False
    if _topic_family(left.get("statement", "")) != _topic_family(right.get("statement", "")):
        return False
    if _canonical_unit(left.get("unit", "")) != _canonical_unit(right.get("unit", "")):
        return False
    first, second = _claim_terms(left.get("statement", "")), _claim_terms(right.get("statement", ""))
    shared = first & second
    # Require a specific shared metric/context. Generic words like "jobs" can
    # refer to unrelated counts within one report.
    return len(shared) >= 2 or bool(shared & {"forecast", "exports", "represented", "budget", "rate", "inflation", "receipts", "stays", "overdue", "applications", "workers"})


def _offline_extract(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Reuse the local, bounded clustering pass for exact routes and safe
    # multilingual headline matches across different source URLs.
    groups = group_articles(articles, _group_key, _day)

    stories = []
    for group in groups:
        group.sort(key=lambda a: _day(a), reverse=True)
        lead = group[0]
        all_articles = []
        facts: list[dict[str, Any]] = []
        for article in group:
            article_id = str(article.get("article_id", ""))
            all_articles.append(_article_reference(article))
            body = str(article.get("body", ""))[:12000]
            # Require a nearby topical word, so arbitrary numbers (IDs, dates)
            # do not become candidate claims.
            for match in list(_NUM.finditer(body))[:40]:
                starts = [m.end() for m in re.finditer(r"[.!?]\s+", body[:match.start()])]
                ends = [m.start() for m in re.finditer(r"[.!?](?:\s+|$)", body[match.end():])]
                left = starts[-1] if starts else 0
                right = match.end() + (ends[0] if ends else len(body[match.end():]))
                sentence = body[left:right].strip()
                context = sentence.lower()
                if not _WORDS.search(context):
                    continue
                val = match.group(0).strip()
                number = _NUMBER.search(val)
                raw_number = number.group(0) if number else ""
                # Calendar years often occur in metric sentences but are not
                # themselves the reported amount.
                if raw_number.isdigit() and len(raw_number) == 4 and 1900 <= int(raw_number) <= 2100 and not val[number.end():].strip():
                    continue
                unit = val[number.end():].strip() if number else ""
                short_evidence = _short_evidence(body, match.start(), match.start(), match.end())
                fact_id = _fact_id(article_id, short_evidence, raw_number, unit, _language(article))
                facts.append({"id": fact_id, "statement": short_evidence, "value": raw_number, "unit": unit, "evidence": short_evidence, "article_id": article_id, "language": _language(article), "source_sentence_hash": _sentence_hash(sentence), "conflict_status": "supported", "_claim_group": (article_id, left)})
        conflict_pairs = []
        for index, fact in enumerate(facts):
            value, unit = _value_unit(fact["value"], fact["unit"], fact["language"])
            for other in facts[index + 1:]:
                other_value, other_unit = _value_unit(other["value"], other["unit"], other["language"])
                if (value, unit) == (other_value, other_unit) or not _claims_contradict(fact, other):
                    continue
                fact["conflict_status"] = other["conflict_status"] = "conflict"
                conflict_pairs.append(tuple(sorted((fact["id"], other["id"]))))
        conflict_group_by_pair = {}
        for pair in conflict_pairs:
            group_id = "conflict-" + hashlib.sha1("|".join(pair).encode("utf-8")).hexdigest()[:10]
            conflict_group_by_pair[pair] = group_id
        for fact in facts:
            if fact["conflict_status"] == "conflict":
                peers = sorted(other["id"] for other in facts if other is not fact and other["conflict_status"] == "conflict" and _claims_contradict(fact, other) and _value_unit(fact["value"], fact["unit"], fact["language"]) != _value_unit(other["value"], other["unit"], other["language"]))
                pair = tuple(sorted((fact["id"], peers[0]))) if peers else ()
                fact["conflict_group"] = conflict_group_by_pair.get(pair, "")
                fact["conflict_label"] = "Conflicting values reported for the same measure"
        for fact in facts:
            fact.pop("_claim_group", None)
        topic_words = _topics(group)
        member_ids = [str(a.get("article_id", "")) for a in group]
        conflicts = [{"id": fact["conflict_group"], "label": fact["conflict_label"], "fact_ids": sorted(other["id"] for other in facts if other.get("conflict_group") == fact.get("conflict_group"))} for fact in facts if fact.get("conflict_group")]
        # De-duplicate a conflict record shared by multiple facts.
        conflicts = list({row["id"]: row for row in conflicts}.values())
        stories.append({"id": _story_id(member_ids, str(lead.get("title", "")), _day(lead)), "title": str(lead.get("title", "Untitled")), "articles": all_articles,
                        "facts": facts, "topics": topic_words or ["news"], "publication_date": _day(lead), "conflicts": conflicts})
    return stories


def prepare(articles: list[dict[str, Any]], providers: Any) -> list[dict[str, Any]]:
    """Convert article records to story records while preserving source links."""
    if not articles:
        return []
    extracted: dict[str, Any] = {}
    if getattr(providers, "live", False):
        groups = group_articles(articles, _group_key, _day)
        extracted_stories = []
        try:
            for batch in _provider_batches(groups):
                result = providers.extract(batch) or {}
                extracted_stories.extend(result.get("stories", []))
        except Exception:
            # Keep the corpus usable if an opted-in provider call fails midway;
            # local extraction still enforces evidence and numeric provenance.
            return _offline_extract(articles)
        extracted = {"stories": extracted_stories}
    if not extracted.get("stories"):
        return _offline_extract(articles)
    # A live model proposes clusters, but only exact source evidence and
    # quantities found in that article are allowed into the prepared corpus.
    by_id = {str(a.get("article_id")): a for a in articles}
    members_by_article = {}
    for group in group_articles(articles, _group_key, _day):
        member_ids = {str(article.get("article_id", "")) for article in group}
        for article_id in member_ids:
            members_by_article[article_id] = member_ids
    clustered: dict[str, dict[str, Any]] = {}
    for item in extracted["stories"]:
        facts: list[dict[str, Any]] = []
        for raw in item.get("facts", []):
            aid = str(raw.get("article_id", "")); article = by_id.get(aid)
            evidence = str(raw.get("evidence", ""))
            body = str(article.get("body", "")) if article else ""
            evidence_offset = body.find(evidence) if evidence else -1
            if not article or not evidence or evidence_offset < 0:
                continue
            language = _language(article)
            value = str(raw.get("value", "")).strip()
            unit = str(raw.get("unit", "")).strip()
            quantity = _value_unit(value, unit, language)
            if not quantity[0]:
                continue
            evidence_quantities = set()
            for match in _NUM.finditer(evidence):
                number = _NUMBER.search(match.group(0))
                if number:
                    observed_unit = match.group(0)[number.end():].strip()
                    evidence_quantities.add((_canonical_number(number.group(0), language), _canonical_unit(observed_unit)))
            # Values and units must occur together in the cited span. A nearby
            # unrelated number elsewhere in the sentence cannot validate it.
            if quantity not in evidence_quantities:
                continue
            value_number = _NUMBER.search(value)
            evidence_number_at = evidence.find(value_number.group(0)) if value_number else -1
            if evidence_number_at < 0:
                continue
            number_start = evidence_offset + evidence_number_at
            sentence_left, sentence_right = _sentence_bounds(body, number_start)
            sentence = body[sentence_left:sentence_right].strip()
            short_evidence = _short_evidence(body, evidence_offset + evidence_number_at, number_start, number_start + len(value_number.group(0)))
            fact = {"id": _fact_id(aid, short_evidence, value, unit, language), "statement": short_evidence, "value": value_number.group(0), "unit": quantity[1], "evidence": short_evidence, "article_id": aid, "language": language, "source_sentence_hash": _sentence_hash(sentence), "conflict_status": "supported", "_claim_group": (aid, sentence_left)}
            if all(existing["id"] != fact["id"] for existing in facts):
                facts.append(fact)
        if not facts: continue
        cited_ids = {f["article_id"] for f in facts}
        referenced = sorted({member for aid in cited_ids for member in members_by_article.get(aid, {aid})})
        day = max((_day(by_id[aid]) for aid in referenced), default="")
        story_id = _story_id(referenced, str(item.get("title", "")), day)
        refs = [_article_reference(by_id[aid]) for aid in referenced]
        if story_id not in clustered:
            clustered[story_id] = {"id": story_id, "title": str(item.get("title", "Untitled")), "articles": refs, "facts": [], "topics": [], "publication_date": day, "conflicts": []}
        story = clustered[story_id]
        known_facts = {fact["id"] for fact in story["facts"]}
        story["facts"].extend(fact for fact in facts if fact["id"] not in known_facts)
        known_articles = {article["article_id"] for article in story["articles"]}
        story["articles"].extend(article for article in refs if article["article_id"] not in known_articles)
        story["topics"] = sorted(set(story["topics"]) | {str(topic) for topic in item.get("topics", []) if str(topic)})
        story["publication_date"] = max(story["publication_date"], day)
    stories = list(clustered.values())
    # Label only competing values for the same measure and retain the label
    # with the claims so narration and evidence views can surface it.
    for story in stories:
        facts = story["facts"]
        for index, fact in enumerate(facts):
            for other in facts[index + 1:]:
                if _claims_contradict(fact, other) and _value_unit(fact["value"], fact["unit"], fact.get("language", "en")) != _value_unit(other["value"], other["unit"], other.get("language", "en")):
                    fact["conflict_status"] = other["conflict_status"] = "conflict"
                    pair = sorted((fact["id"], other["id"]))
                    group_id = "conflict-" + hashlib.sha1("|".join(pair).encode("utf-8")).hexdigest()[:10]
                    fact["conflict_group"] = other["conflict_group"] = group_id
                    fact["conflict_label"] = other["conflict_label"] = "Conflicting values reported for the same measure"
        story["conflicts"] = list({fact["conflict_group"]: {"id": fact["conflict_group"], "label": fact["conflict_label"], "fact_ids": sorted(other["id"] for other in facts if other.get("conflict_group") == fact.get("conflict_group"))} for fact in facts if fact.get("conflict_group")}.values())
        for fact in facts:
            fact.pop("_claim_group", None)
    return stories or _offline_extract(articles)


def _profile_weights(profile: dict[str, Any]) -> list[tuple[str, float]]:
    # Aliases share story features. Keep the original term order and weights
    # so scores preserve the baseline's summation and tie behavior.
    return [
        (_topic_key(topic), max(0.0, float(weight)))
        for topic, weight in (profile.get("topic_weights") or {}).items()
    ]


def _feedback_weights(
    weights: list[tuple[str, float]],
    stories: list[dict[str, Any]],
    events: Iterable[dict[str, Any]],
) -> list[tuple[str, float]]:
    # Events arrive oldest first. Each story's latest rating contributes once
    # per matching axis. Sum the signed 0.1 steps before clamping.
    latest = {
        event["story_id"]: event["rating"]
        for event in events
        if event.get("story_id") and event.get("rating") in ("up", "down")
    }
    configured_axes = {axis for axis, _ in weights}
    deltas: Counter[str] = Counter()
    for story in stories:
        rating = latest.pop(story.get("id"), None)
        if rating is None:
            continue
        topics = _story_topics(story)
        axes = {axis for axis in configured_axes if _matches_topic(axis, topics)}
        axes.update(topic for topic in topics if not _matches_topic(topic, configured_axes))
        for axis in sorted(axes):
            deltas[axis] += 1 if rating == "up" else -1
    adjusted = [
        (axis, min(1.0, max(0.0, weight + deltas[axis] * FEEDBACK_WEIGHT)) if deltas[axis] else weight)
        for axis, weight in weights
    ]
    adjusted.extend(
        (axis, min(1.0, steps * FEEDBACK_WEIGHT))
        for axis, steps in sorted(deltas.items()) if axis not in configured_axes and steps > 0
    )
    return adjusted


def _unspoken_story(story: dict[str, Any], spoken: set[str]) -> dict[str, Any]:
    facts = [fact for fact in _verified_facts(story) if str(fact["id"]) not in spoken]
    remaining = {fact["id"] for fact in facts}
    conflicts = []
    for conflict in story.get("conflicts", []):
        fact_ids = [fact_id for fact_id in conflict["fact_ids"] if fact_id in remaining]
        if fact_ids:
            conflicts.append({**conflict, "fact_ids": fact_ids})
    return {**story, "facts": facts, "conflicts": conflicts}


@dataclass(frozen=True)
class _StoryFeatures:
    story: dict[str, Any]
    matching_topics: set[str]
    relevance: dict[str, float]
    recency: float
    novelty: float
    trust_reason: str


def _story_features(
    stories: list[dict[str, Any]],
    weight_axes: set[str],
    exclusion_axes: set[str],
    spoken: set[str],
    region: str,
) -> tuple[list[_StoryFeatures], set[str]]:
    reference_day = max((str(story.get("publication_date", ""))[:10] for story in stories), default="")
    novelty = _novelty_values(stories)
    vocabulary = {
        axis: tuple(re.compile(rf"(?<![\w]){re.escape(term)}[\w-]*(?![\w])") for term in _topic_terms(axis))
        for axis in sorted(weight_axes)
    }
    vocabulary = {axis: patterns for axis, patterns in vocabulary.items() if patterns}
    axes = weight_axes | exclusion_axes
    rows = []
    for story in stories:
        if not _matches_region(story, region):
            continue
        eligible, trust_reason = _trust_eligible(story)
        if not eligible:
            continue
        if spoken:
            story = _unspoken_story(story, spoken)
            eligible, trust_reason = _trust_eligible(story)
            if not eligible:
                continue
        topics = _story_topics(story)
        content = " ".join(
            [str(story.get("title", ""))]
            + [str(fact.get("statement", "")) for fact in story.get("facts", [])]
        ).lower()
        relevance = {
            axis: sum(bool(pattern.search(content)) for pattern in patterns) / len(patterns)
            for axis, patterns in vocabulary.items()
        }
        rows.append(_StoryFeatures(
            story, {axis for axis in axes if _matches_topic(axis, topics)}, relevance,
            _recency(story, reference_day), novelty.get(story["id"], 0.0), trust_reason,
        ))
    return rows, set(vocabulary)


def _rank_batch(
    stories: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    feedback_events: Iterable[dict[str, Any]] = (),
    spoken: set[str] | None = None,
    region: str = "all",
) -> list[list[dict[str, Any]]]:
    events = list(feedback_events)
    weights = [_profile_weights(profile) for profile in profiles]
    if events:
        weights = [_feedback_weights(row, stories, events) for row in weights]
    exclusions = [{_topic_key(topic) for topic in profile.get("excluded_topics", [])} for profile in profiles]
    features, relevance_axes = _story_features(
        stories, {axis for row in weights for axis, _ in row}, set().union(*exclusions), spoken or set(), region,
    )
    # Sparse dot products over shared topic axes. Story parsing, vocabulary
    # matching, time and duplicate scores are computed once for the batch.
    return [
        _rank_profile(features, row, excluded, relevance_axes)
        for row, excluded in zip(weights, exclusions)
    ]


def _rank_profile(
    features: list[_StoryFeatures],
    weights: list[tuple[str, float]],
    excluded: set[str],
    relevance_axes: set[str],
) -> list[dict[str, Any]]:
    topic_total = sum(weight for _, weight in weights)
    relevance_weights = [(axis, weight) for axis, weight in weights if axis in relevance_axes]
    relevance_total = 0.0
    for _, weight in relevance_weights:
        relevance_total += weight
    rows = []
    for feature in features:
        if excluded & feature.matching_topics:
            continue
        story = feature.story
        matching = [(axis, weight) for axis, weight in weights if axis in feature.matching_topics and weight > 0]
        matched_weight = 0.0
        for _, weight in matching:
            matched_weight += weight
        weighted_hits = 0.0
        for axis, weight in relevance_weights:
            weighted_hits += feature.relevance[axis] * weight
        topic_match = min(1.0, matched_weight / topic_total) if topic_total else 0.0
        relevance = min(1.0, weighted_hits / relevance_total) if relevance_total else 0.0
        score = 0.4 * topic_match + 0.3 * relevance + 0.2 * feature.recency + 0.1 * feature.novelty
        why = "Sourced recent story"
        related = matching or [
            (axis, feature.relevance[axis] * weight) for axis, weight in relevance_weights
            if feature.relevance[axis] and weight > 0
        ]
        if related:
            contributions: dict[str, float] = {}
            for axis, amount in related:
                contributions[axis] = contributions.get(axis, 0.0) + amount
            topics = sorted(contributions, key=lambda axis: (-contributions[axis], axis))[:3]
            why = "Matches your interest in " + ", ".join(topics)
        if any(f.get("conflict_status") == "conflict" for f in story.get("facts", [])):
            why += ". Conflicting figures are labeled"
        rows.append((score, {
            **story,
            "trust_eligible": True,
            "trust_reason": feature.trust_reason,
            "score_components": {
                "topic_match": round(topic_match, 4),
                "relevance": round(relevance, 4),
                "recency": round(feature.recency, 4),
                "novelty": round(feature.novelty, 4),
                "score": round(score, 4),
            },
            "reason": why,
        }))
    rows.sort(key=lambda row: (row[0], row[1].get("publication_date", ""), row[1].get("id", "")), reverse=True)
    return [story for _, story in rows]


def rank(
    stories: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    feedback_events: Iterable[dict[str, Any]] | None = None,
    spoken_fact_ids: Iterable[str] | None = None,
    region: str = "all",
) -> list[dict[str, Any]]:
    """Rank with listener-scoped events and IDs from the preceding seven days.

    The caller owns listener identity and expiration. Feedback must be in
    chronological order or already reduced to the latest rating per story.
    """
    region = region.strip().lower()
    if region not in {"all", "europe"} and not re.fullmatch(r"[a-z]{2}", region):
        raise ValueError("Region must be all, Europe, or an ISO alpha-2 country code")
    return _rank_batch(stories, [profile], feedback_events or (), set(spoken_fact_ids or ()), region)[0]


def rank_profiles(
    stories: list[dict[str, Any]], profiles: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Rank all configured profiles without any listener-specific inputs."""
    configured = list(profiles)
    if not configured:
        return {}
    return {profile["id"]: rows for profile, rows in zip(configured, _rank_batch(stories, configured))}


def _quantity_supported(fact: dict[str, Any], language: str) -> bool:
    expected = _value_unit(fact.get("value", ""), fact.get("unit", ""), language)
    if not expected[0]:
        return False
    for match in _NUM.finditer(str(fact.get("evidence", ""))):
        number = _NUMBER.search(match.group(0))
        if number:
            unit = match.group(0)[number.end():].strip()
            if (_canonical_number(number.group(0), language), _canonical_unit(unit)) == expected:
                return True
    return False


def _verified_facts(story: dict[str, Any]) -> list[dict[str, Any]]:
    articles = {str(article.get("article_id")): article for article in story.get("articles", [])}
    facts = []
    for fact in story.get("facts", []):
        article = articles.get(str(fact.get("article_id", "")))
        if fact.get("id") and article and fact.get("evidence"):
            language = str(fact.get("language") or article.get("language", "en"))
            if _quantity_supported(fact, language):
                facts.append(fact)
    return facts


_SPELLED_NUMBERS = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|hundred|thousand|million|billion|null|eins|zwei|drei|vier|f[uü]nf|sechs|sieben|acht|neun|zehn|elf|zw[oö]lf|zwanzig|dreißig|vierzig|f[uü]nfzig|hundert|tausend|een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien|twintig|dertig|honderd|duizend)\b", re.I)


def _script_sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", " ".join(text.split()))


def _script_is_valid(
    candidate: str,
    facts: list[dict[str, Any]],
    approved: list[str],
) -> bool:
    text = str(candidate or "").strip()
    without_units = re.sub(_UNIT_PATTERN, " ", text, flags=re.I)
    if not text or re.search(r"\b(?:fact|story)-[a-z0-9-]+\b", text, re.I) or _SPELLED_NUMBERS.search(without_units):
        return False
    source_hashes = {str(f.get("source_sentence_hash")) for f in facts if f.get("source_sentence_hash")}
    sentences = _script_sentences(text)
    if any(_sentence_hash(sentence) in source_hashes for sentence in sentences):
        return False
    return sentences[0] == approved[0] and Counter(sentences[1:]) == Counter(approved[1:])


_MEASURE_LABELS = (
    (r"revenue|turnover|umsatz|omzet", {"en": "revenue", "de": "Umsatz", "nl": "omzet"}),
    (r"(?:operating )?profit|gewinn|winst", {"en": "profit", "de": "Gewinn", "nl": "winst"}),
    (r"growth forecast|wachstumsprognose|groeiverwachting", {"en": "growth forecast", "de": "Wachstumsprognose", "nl": "groeiverwachting"}),
    (r"policy rate|leitzins|beleidsrente", {"en": "policy rate", "de": "Leitzins", "nl": "beleidsrente"}),
    (r"basis points?|basispunkte|basispunten", {"en": "rate change", "de": "Zinsänderung", "nl": "renteverandering"}),
    (r"inflati(?:on|e)|teuerung", {"en": "inflation", "de": "Inflation", "nl": "inflatie"}),
    (r"pay rise|wage|lohn|loon", {"en": "pay increase", "de": "Lohnerhöhung", "nl": "loonsverhoging"}),
    (r"jobs?|arbeitsplätze|banen|workers?|arbeitnehmer|werknemers", {"en": "job or worker count", "de": "Beschäftigtenzahl", "nl": "aantal banen"}),
    (r"application|anträge|aanvragen|equipment grant|investitionszuschuss|investeringssubsidie", {"en": "grant applications", "de": "Förderanträge", "nl": "subsidieaanvragen"}),
    (r"saving rate|savings|sparquote|spaarquote", {"en": "saving rate", "de": "Sparquote", "nl": "spaarquote"}),
    (r"food prices|lebensmittelpreise|voedselprijzen", {"en": "food prices", "de": "Lebensmittelpreise", "nl": "voedselprijzen"}),
    (r"solar capacity|solarkapazität|photovoltaikkapazität|zonnecapaciteit", {"en": "solar capacity", "de": "Solarkapazität", "nl": "zonnecapaciteit"}),
    (r"per megawatt|weighted average price|contract bid", {"en": "contract price", "de": "Vertragspreis", "nl": "contractprijs"}),
    (r"tourism receipts|tourismus|toerisme|touristische einnahmen", {"en": "tourism receipts", "de": "Tourismuseinnahmen", "nl": "toeristische inkomsten"}),
    (r"overnight stays|nächtigungen|overnachtingen", {"en": "overnight stays", "de": "Übernachtungen", "nl": "overnachtingen"}),
    (r"credit line|loan guarantee|kreditlinie|kredietlijn|loans?|kredite|leningen", {"en": "business credit", "de": "Unternehmenskredit", "nl": "bedrijfskrediet"}),
    (r"waiting time|wartezeit|wachttijd", {"en": "waiting time", "de": "Wartezeit", "nl": "wachttijd"}),
    (r"overdue|überfällig|achterstallig", {"en": "overdue farm loans", "de": "überfällige Agrarkredite", "nl": "achterstallige landbouwleningen"}),
)


def _measure_label(fact: dict[str, Any], source_language: str, language: str) -> str:
    text = str(fact.get("evidence", "")).lower()
    quantity = _value_unit(fact.get("value", ""), fact.get("unit", ""), source_language)
    amounts = list(_NUM.finditer(text))
    candidates = []
    for pattern, labels in _MEASURE_LABELS:
        for match in re.finditer(pattern, text):
            # Anchor each label to the nearest quantity, so another figure
            # in the same sentence cannot inherit this measure's label.
            closest = min(
                amounts,
                key=lambda amount: max(0, match.start() - amount.end(), amount.start() - match.end()),
            )
            if _value_unit(closest.group(0), language=source_language) == quantity:
                distance = max(0, match.start() - closest.end(), closest.start() - match.end())
                candidates.append((distance, labels.get(language, labels["en"])))
    if candidates:
        return min(candidates, key=lambda candidate: candidate[0])[1]
    return {"de": "berichtete Kennzahl", "nl": "gerapporteerd cijfer"}.get(language, "reported measure")


def _spoken_number(value: str, source_language: str, output_language: str) -> str:
    token = _NUMBER.search(value).group(0)
    if (source_language in {"de", "nl"}) == (output_language in {"de", "nl"}):
        return token
    if output_language in {"de", "nl"}:
        return token.replace(",", "").replace(".", ",")
    return token.replace(".", "").replace(",", ".")


_NARRATION_UNITS = (
    ({"percent"}, {"en": "percent", "de": "Prozent", "nl": "procent"}),
    ({"basis points", "basispunkte", "basispunten"}, {"en": "basis points", "de": "Basispunkte", "nl": "basispunten"}),
    ({"applications", "application", "anträge", "aanvragen"}, {"en": "applications", "de": "Anträge", "nl": "aanvragen"}),
)

_NARRATION_FRAMES = {
    "en": {"beginner": "In plain terms, here’s what the sources report.", "intermediate": "Here’s what the sources report.", "advanced": "Here are the reported measures."},
    "de": {"beginner": "Einfach gesagt steht Folgendes in den Quellen.", "intermediate": "Das berichten die Quellen.", "advanced": "Das sind die berichteten Kennzahlen."},
    "nl": {"beginner": "Eenvoudig gezegd melden de bronnen het volgende.", "intermediate": "Dit melden de bronnen.", "advanced": "Dit zijn de gerapporteerde cijfers."},
}


def narrate(ranked: list[dict[str, Any]], profile: dict[str, Any], providers: Any) -> list[dict[str, Any]]:
    """Create short segments from validated facts; never introduce new figures."""
    result = []
    duration = max(1, int(profile.get("duration", 5) or 5))
    output_language = str(profile.get("language", "en")).lower()
    expertise = str(profile.get("expertise", "intermediate")).lower()
    style = str(profile.get("style") or "neutral")
    style_words = style.lower()
    lead_with_figures = any(phrase in style_words for phrase in ("lead with", "concise", "focus on drivers"))
    plain_language = any(phrase in style_words for phrase in ("plain language", "everyday decisions"))
    frame = _NARRATION_FRAMES.get(output_language, _NARRATION_FRAMES["en"])
    introduction = frame.get(expertise, frame["intermediate"])
    for story in ranked:
        by_id = {str(a.get("article_id")): a for a in story.get("articles", [])}
        facts = _verified_facts(story)
        if not facts:
            continue
        ids = list(dict.fromkeys(str(f["id"]) for f in facts))
        cited_ids = list(dict.fromkeys(str(f.get("article_id")) for f in facts))
        sources = [by_id[aid] for aid in cited_ids if aid in by_id]
        conflicted = any(f.get("conflict_status") == "conflict" for f in facts)
        source_languages = {
            fact["id"]: str(fact.get("language") or by_id[str(fact["article_id"])].get("language", "en"))
            for fact in facts
        }
        # Equivalent source facts share a spoken measure and retain all of
        # their citations. Equal values for different measures stay distinct.
        preferred = sorted(facts, key=lambda fact: source_languages[fact["id"]] != output_language)
        approved = [introduction]
        seen: set[tuple[str, str, str]] = set()
        for fact in preferred:
            source_language = source_languages[fact["id"]]
            value, unit = _value_unit(fact["value"], fact.get("unit", ""), source_language)
            label = _measure_label(fact, source_language, output_language)
            spoken_unit = next(
                (labels.get(output_language, labels["en"]) for aliases, labels in _NARRATION_UNITS if unit in aliases),
                str(fact.get("unit") or unit),
            )
            key = (label, value, spoken_unit.casefold())
            if key in seen:
                continue
            seen.add(key)
            number = _spoken_number(str(fact["value"]), source_language, output_language)
            amount = f"{number} {spoken_unit}".strip()
            heading = label[:1].upper() + label[1:]
            if lead_with_figures:
                approved.append(f"{amount} ({label}).")
            elif plain_language:
                verb = {"de": "lag bei", "nl": "was"}.get(output_language, "was")
                approved.append(f"{heading} {verb} {amount}.")
            else:
                approved.append(f"{heading}, {amount}.")
        text = " ".join(approved)
        conflict_text = {"en": "The reports give conflicting values.", "de": "Die Berichte nennen unterschiedliche Werte.", "nl": "De berichten noemen verschillende waarden."}.get(output_language, "The reports give conflicting values.")
        # Let the live model order the verified sentences. Keeping the claim
        # wording fixed prevents attaching a valid amount to a new claim.
        if getattr(providers, "live", False):
            sentences = "\n".join(approved)
            prompt = (
                f"Arrange these verified narration sentences in language {output_language} as one paragraph. "
                "Keep the introduction first and include every remaining sentence exactly once with its wording "
                "unchanged. Add nothing. "
                f"Listener expertise: {expertise}. Listener style: {style}.\n{sentences}"
            )
            candidate = providers.generate_script(prompt, output_language)
            if _script_is_valid(candidate, facts, approved):
                text = " ".join(_script_sentences(candidate))
        if conflicted and conflict_text.lower() not in text.lower():
            text += " " + conflict_text
        result.append({"id": "segment-" + story["id"], "text": text, "fact_ids": ids, "sources": sources, "conflict": conflicted, "conflict_labels": list(dict.fromkeys(f.get("conflict_label") for f in facts if f.get("conflict_label")))})
        if len(result) >= duration:
            break
    return result


def evaluate(stories: list[dict[str, Any]], answer_key: dict[str, Any]) -> dict[str, Any]:
    """Compact fixture/data-quality evaluation against expected values."""
    all_facts = [f for story in stories for f in story.get("facts", [])]
    expected = answer_key.get("facts", answer_key) if isinstance(answer_key, dict) else {}
    errors = []
    for key, value in expected.items():
        matches = [f for f in all_facts if f.get("id") == key or f.get("evidence") == key]
        if not matches or all(str(value) not in str(f.get("value", "")) for f in matches):
            errors.append({"expected": key, "value": value, "found": [f.get("value") for f in matches]})
    return {"story_count":len(stories), "fact_count":len(all_facts), "conflict_count":sum(f.get("conflict_status")=="conflict" for f in all_facts), "checks":len(expected), "errors":errors, "passed":not errors}
