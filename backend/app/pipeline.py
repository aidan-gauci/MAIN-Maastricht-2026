"""Deterministic news preparation, ranking, and factual narration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
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


def _topic_key(value: Any) -> str:
    text = str(value).strip().lower()
    return _TOPIC_ALIASES.get(text, text)


def _topic_terms(value: str) -> tuple[str, ...]:
    key = _topic_key(value)
    terms = _TOPIC_TERMS.get(key)
    if terms:
        return terms
    return tuple(re.findall(r"[a-z0-9]+", key))


def _topic_match(profile: dict[str, Any], story: dict[str, Any]) -> float:
    weights = profile.get("topic_weights") or {}
    if not weights:
        return 0.0
    story_topics = {_topic_key(topic) for topic in story.get("topics", [])}
    total = sum(max(0.0, float(weight)) for weight in weights.values())
    if not total:
        return 0.0
    matched = 0.0
    for topic, weight in weights.items():
        key = _topic_key(topic)
        if any(key == candidate or key in candidate or candidate in key for candidate in story_topics):
            matched += max(0.0, float(weight))
    return min(1.0, matched / total)


def _relevance(profile: dict[str, Any], story: dict[str, Any]) -> float:
    """Measure how much of the listener's chosen vocabulary is in the story."""
    weights = profile.get("topic_weights") or {}
    if not weights:
        return 0.0
    text = " ".join([str(story.get("title", ""))] + [str(f.get("statement", "")) for f in story.get("facts", [])]).lower()
    weighted_hits = 0.0
    total = 0.0
    for topic, weight in weights.items():
        terms = _topic_terms(str(topic))
        if not terms:
            continue
        score = sum(1 for term in terms if re.search(rf"(?<![\w]){re.escape(term)}[\w-]*(?![\w])", text)) / len(terms)
        amount = max(0.0, float(weight))
        weighted_hits += score * amount
        total += amount
    return min(1.0, weighted_hits / total) if total else 0.0


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
            all_articles.append({"article_id": article_id, "title": article.get("title", ""), "source": article.get("source", ""), "url": article.get("url", ""), "language": _language(article), "official": bool(article.get("official", False))})
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
        refs = [{"article_id": aid, "title": by_id[aid].get("title", ""), "source": by_id[aid].get("source", ""), "url": by_id[aid].get("url", ""), "language": _language(by_id[aid]), "official": bool(by_id[aid].get("official", False))} for aid in referenced]
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


def rank(stories: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the fixed topic, relevance, recency, and novelty score."""
    if not stories: return []
    ref = max((str(s.get("publication_date", ""))[:10] for s in stories), default="")
    excluded = {_topic_key(topic) for topic in profile.get("excluded_topics", [])}
    novelty_values = _novelty_values(stories)
    rows = []
    for story in stories:
        topics = {_topic_key(topic) for topic in story.get("topics", [])}
        if any(any(blocked == topic or blocked in topic or topic in blocked for topic in topics) for blocked in excluded):
            continue
        eligible, trust_reason = _trust_eligible(story)
        if not eligible:
            continue
        topic_match = _topic_match(profile, story)
        relevance = _relevance(profile, story)
        recency = _recency(story, ref)
        novelty = novelty_values.get(story["id"], 0.0)
        conflict = any(f.get("conflict_status") == "conflict" for f in story.get("facts", []))
        score = 0.4 * topic_match + 0.3 * relevance + 0.2 * recency + 0.1 * novelty
        why = "Matches selected topics" if topic_match else "Sourced recent story"
        if conflict:
            why += "; conflicting figures are labeled"
        item = dict(story)
        item["trust_eligible"] = True
        item["trust_reason"] = trust_reason
        item["score_components"] = {"topic_match": round(topic_match, 4), "relevance": round(relevance, 4), "recency": round(recency, 4), "novelty": round(novelty, 4), "score": round(score, 4)}
        item["reason"] = why
        item["_score"] = score
        rows.append(item)
    rows.sort(key=lambda x: (x["_score"], x.get("publication_date", ""), x.get("id", "")), reverse=True)
    for row in rows: row.pop("_score", None)
    return rows


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


_SPELLED_NUMBERS = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|hundred|thousand|million|billion|null|eins|zwei|drei|vier|f[uü]nf|sechs|sieben|acht|neun|zehn|elf|zw[oö]lf|zwanzig|dreißig|vierzig|f[uü]nfzig|hundert|tausend|een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien|twintig|dertig|honderd|duizend)\b", re.I)


def _script_is_valid(candidate: str, facts: list[dict[str, Any]], language: str) -> bool:
    text = str(candidate or "").strip()
    without_units = re.sub(_UNIT_PATTERN, " ", text, flags=re.I)
    if not text or re.search(r"\b(?:fact|story)-[a-z0-9-]+\b", text, re.I) or _SPELLED_NUMBERS.search(without_units):
        return False
    source_hashes = {str(f.get("source_sentence_hash")) for f in facts if f.get("source_sentence_hash")}
    if any(_sentence_hash(sentence) in source_hashes for sentence in re.split(r"(?<=[.!?])\s+", text)):
        return False
    allowed = {_value_unit(f.get("value", ""), f.get("unit", ""), str(f.get("language", language))) for f in facts}
    mentions = []
    for match in _NUM.finditer(text):
        number = _NUMBER.search(match.group(0))
        if number:
            unit = match.group(0)[number.end():].strip()
            mentions.append((_canonical_number(number.group(0), language), _canonical_unit(unit)))
    return bool(mentions) and all(mention in allowed for mention in mentions)


_MEASURE_LABELS = (
    (r"growth forecast|wachstumsprognose|groeiverwachting", {"en": "growth forecast", "de": "Wachstumsprognose", "nl": "groeiverwachting"}),
    (r"policy rate|leitzins|beleidsrente", {"en": "policy rate", "de": "Leitzins", "nl": "beleidsrente"}),
    (r"basis points?|basispunkte|basispunten", {"en": "rate change", "de": "Zinsänderung", "nl": "renteverandering"}),
    (r"inflation|teuerung", {"en": "inflation", "de": "Inflation", "nl": "inflatie"}),
    (r"pay rise|wage|lohn|loon", {"en": "pay increase", "de": "Lohnerhöhung", "nl": "loonsverhoging"}),
    (r"jobs?|arbeitsplätze|banen|workers?|arbeitnehmer|werknemers", {"en": "job or worker count", "de": "Beschäftigtenzahl", "nl": "aantal banen"}),
    (r"application|anträge|aanvragen|equipment grant|investitionszuschuss|investeringssubsidie", {"en": "grant applications", "de": "Förderanträge", "nl": "subsidieaanvragen"}),
    (r"saving rate|savings|sparquote|spaarquote", {"en": "saving rate", "de": "Sparquote", "nl": "spaarquote"}),
    (r"food prices|lebensmittelpreise|voedselprijzen", {"en": "food prices", "de": "Lebensmittelpreise", "nl": "voedselprijzen"}),
    (r"solar capacity|megawatts?|solar|photovoltaik|zonne-energie", {"en": "solar capacity", "de": "Solarkapazität", "nl": "zonnecapaciteit"}),
    (r"per megawatt|weighted average price|contract bid|auktion|auction", {"en": "contract price", "de": "Vertragspreis", "nl": "contractprijs"}),
    (r"tourism receipts|tourismus|toerisme|touristische einnahmen", {"en": "tourism receipts", "de": "Tourismuseinnahmen", "nl": "toeristische inkomsten"}),
    (r"overnight stays|nächtigungen|overnachtingen", {"en": "overnight stays", "de": "Übernachtungen", "nl": "overnachtingen"}),
    (r"credit line|loan guarantee|kreditlinie|kredietlijn|loans?|kredite|leningen", {"en": "business credit", "de": "Unternehmenskredit", "nl": "bedrijfskrediet"}),
    (r"waiting time|wartezeit|wachttijd", {"en": "waiting time", "de": "Wartezeit", "nl": "wachttijd"}),
    (r"overdue|überfällig|achterstallig", {"en": "overdue farm loans", "de": "überfällige Agrarkredite", "nl": "achterstallige landbouwleningen"}),
)


def _measure_label(fact: dict[str, Any], language: str, story_topics: list[str]) -> str:
    text = str(fact.get("evidence", "")).lower()
    for pattern, labels in _MEASURE_LABELS:
        if re.search(pattern, text, re.I):
            return labels.get(language, labels["en"])
    topic = _topic_key(next(iter(story_topics), "reported measure"))
    topic_labels = {
        "growth": {"en": "growth forecast", "de": "Wachstumsprognose", "nl": "groeiverwachting"},
        "rates": {"en": "rate", "de": "Zinssatz", "nl": "rente"},
        "industry": {"en": "industry grant", "de": "Industrieförderung", "nl": "industriesubsidie"},
        "energy": {"en": "energy figure", "de": "Energiewert", "nl": "energiecijfer"},
        "tourism": {"en": "tourism figure", "de": "Tourismuswert", "nl": "toerismecijfer"},
    }
    return topic_labels.get(topic, {}).get(language, "reported measure")


def narrate(ranked: list[dict[str, Any]], profile: dict[str, Any], providers: Any) -> list[dict[str, Any]]:
    """Create short segments from validated facts; never introduce new figures."""
    result = []
    duration = max(1, int(profile.get("duration", 5) or 5))
    limit = max(1, min(len(ranked), duration))
    output_language = str(profile.get("language", "en")).lower()
    for story in ranked[:limit]:
        by_id = {str(a.get("article_id")): a for a in story.get("articles", [])}
        facts = [fact for fact in story.get("facts", []) if fact.get("id") and str(fact.get("article_id", "")) in by_id and fact.get("evidence") and _quantity_supported(fact, str(fact.get("language") or by_id[str(fact["article_id"])].get("language", "en")))]
        if not facts:
            continue
        ids = list(dict.fromkeys(str(f["id"]) for f in facts))
        cited_ids = list(dict.fromkeys(str(f.get("article_id")) for f in facts))
        sources = [by_id[aid] for aid in cited_ids if aid in by_id]
        conflicted = any(f.get("conflict_status") == "conflict" for f in facts)
        # Prefer a number's original output-language form, then deduplicate
        # cross-language editions by their canonical quantity.
        preferred = sorted(facts, key=lambda f: str(f.get("language", by_id[str(f["article_id"])].get("language", "en"))) != output_language)
        unique = []
        seen = set()
        for fact in preferred:
            source_language = str(fact.get("language", by_id[str(fact["article_id"])].get("language", "en")))
            key = _value_unit(fact.get("value", ""), fact.get("unit", ""), source_language)
            if key not in seen:
                seen.add(key)
                unique.append(fact)
        claims = "; ".join(
            f"{_measure_label(f, output_language, story.get('topics', []))}: {f.get('value', '')} {f.get('unit', '')}".strip()
            for f in unique if f.get("value")
        )
        expertise = str(profile.get("expertise", "intermediate"))
        frames = {
            "en": {"beginner": f"In plain terms, the reports give these measures: {claims}.", "intermediate": f"The reports give these measures: {claims}.", "advanced": f"Reported measures: {claims}."},
            "de": {"beginner": f"Einfach gesagt, nennen die Berichte diese Werte: {claims}.", "intermediate": f"Die Berichte nennen diese Werte: {claims}.", "advanced": f"Berichtete Kennzahlen: {claims}."},
            "nl": {"beginner": f"Eenvoudig gezegd noemen de berichten deze cijfers: {claims}.", "intermediate": f"De berichten noemen deze cijfers: {claims}.", "advanced": f"Gerapporteerde cijfers: {claims}."},
        }
        text = frames.get(output_language, frames["en"]).get(expertise, frames.get(output_language, frames["en"])["intermediate"])
        conflict_text = {"en": "The reports give conflicting values.", "de": "Die Berichte nennen unterschiedliche Werte.", "nl": "De berichten noemen verschillende waarden."}.get(output_language, "The reports give conflicting values.")
        # Draft from the verified amount list only. The validator rejects new
        # quantities, number words, metadata IDs, and exact source sentences.
        if getattr(providers, "live", False):
            context_rows = "\n".join(f"metadata id={f['id']} (never speak this ID); source context={f.get('evidence', '')}; verified amount={f.get('value', '')} {f.get('unit', '')}" for f in unique)
            prompt = f"Write a concise paraphrase in language {output_language}. Use only these verified amounts, units, and bounded contexts. Add no other numbers or number words. Do not speak metadata IDs or copy any full article sentence. Listener expertise: {expertise}; style: {profile.get('style', 'neutral')}\n{context_rows}\nConflicting reports: {conflicted}"
            candidate = providers.generate_script(prompt, profile.get("language", "en"))
            if _script_is_valid(candidate, facts, output_language):
                text = candidate.strip()
        if conflicted and conflict_text.lower() not in text.lower():
            text += " " + conflict_text
        result.append({"id": "segment-" + story["id"], "text": text, "fact_ids": ids, "sources": sources, "conflict": conflicted, "conflict_labels": list(dict.fromkeys(f.get("conflict_label") for f in facts if f.get("conflict_label")))})
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
