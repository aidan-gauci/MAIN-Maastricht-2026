"""Deterministic news preparation, ranking, and factual narration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from heapq import nlargest
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit

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
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower() in {"en", "de", "nl"}:
        parts.pop(0)
    route = "/".join(parts)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    # Some feeds use a shared landing page or query-based article IDs.
    if parsed.netloc and (query or route.casefold() not in {"", "news", "latest", "index.html"}):
        return f"url|{parsed.netloc.casefold()}|{route}?{urlencode(query)}"
    return f"article|{article['article_id']}"


# These phrases identify a measure more precisely than isolated financial words.
_MEASURES = {
    "operating profit": r"operating profit|operatives ergebnis|betriebsgewinn|gewinn aus dem betrieb|operationele winst|bedrijfswinst",
    "policy rate": r"policy rate|leitzins|beleidsrente",
    "inflation": r"inflation|inflatie|teuerung",
}
_MEASURE_PATTERNS = {name: re.compile(rf"\b(?:{terms})\b", re.I) for name, terms in _MEASURES.items()}


def _topic_family(context: str) -> str:
    words = [w.lower() for w in _WORDS.findall(context)]
    for family, terms in (("revenue", ("revenue", "sales", "turnover", "umsatz", "omzet")),
                          ("profit", ("profit", "winst", "gewinn")),
                          ("rate", ("rate",)), ("inflation", ("inflation",))):
        if any(w in terms for w in words): return family
    return words[0] if words else "figure"


# Match whole words and explicit inflections so "reports" is not a port story
# and Dutch "rente" does not imply housing.
_TOPIC_TERMS = {
    "growth": r"growth|forecasts?|wachstum(?:sprognose)?|prognose|groei(?:verwachting)?",
    "trade": r"exports?|exporters?|exporte|trade|handel",
    "rates": r"rates?|rente|beleidsrente|leitzins|interest rates?|basis points?|basispunkte?n?|basispunten",
    "inflation": r"inflation|inflatie|prices?|preise|prijzen",
    "jobs": r"jobs?|workers?|arbeitsplatz|arbeitsplätze|banen|wages?|lohn|loon|dockworkers?|employment",
    "energy": r"energy|solar|megawatts?|electricity|energie",
    "housing": r"housing|rents?|rentals?|homes?|households?|haushalte?|huishoudens?",
    "industry": r"factor(?:y|ies)|manufactur(?:e|er|ers|ing)|industry|industrie|grants?|equipment",
    "tourism": r"tourism|tourismus|toerisme|visitors?|receipts|stays|nächtigungen",
    "agriculture": r"farms?|farmers?|farming|agriculture|fertilizers?|feed|landwirtschaft",
    "credit": r"loans?|credit|lenders?|kredite?|krediet|payments?|overdue",
    "transport": r"rail|freight|ports?|harbou?rs?|hafen|haven|borders?",
    "markets": r"markets?|banks?|auction|investments?|exports?|trade",
    "earnings": r"earnings|profits?|revenue|winst|gewinn|umsatz|omzet|quartalsumsatz|kwartaalomzet",
    "technology": r"technology|technologie|software|semiconductors?",
}
_TOPIC_PATTERNS = {topic: re.compile(rf"\b(?:{terms})\b", re.I) for topic, terms in _TOPIC_TERMS.items()}
_TOPIC_ALIASES = {"rate": "rates", "interest rate": "rates", "interest rates": "rates", "trade and exports": "trade", "prices": "inflation", "employment": "jobs", "transportation": "transport"}


def _topic_key(value: Any) -> str:
    text = " ".join(str(value).casefold().split())
    return _TOPIC_ALIASES.get(text, text)


def _has_topic(key: str, labels: set[str]) -> bool:
    return bool(key) and any(re.search(rf"(?<!\w){re.escape(key)}(?!\w)", label) for label in labels)


def _mentions_topic(key: str, text: str) -> bool:
    pattern = _TOPIC_PATTERNS.get(key)
    if pattern:
        return bool(pattern.search(text))
    return bool(re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text, re.I))


def _positive_topic_weights(profile: dict[str, Any]) -> dict[str, float]:
    return {topic: float(weight) for topic, weight in (profile.get("topic_weights") or {}).items()
            if _topic_key(topic) and float(weight) > 0}


class _RankingCandidate(NamedTuple):
    story: dict[str, Any]
    labels: set[str]
    trust_reason: str
    text: str
    known_topics: dict[str, tuple[bool, bool]]


def _topic_matches(key, candidate):
    if key in candidate.known_topics:
        return candidate.known_topics[key]
    return _has_topic(key, candidate.labels), _mentions_topic(key, candidate.text)


def _profile_scores(interests, total, candidate):
    """Topic match uses labels. Relevance uses interest matches in sourced text."""
    if not total:
        return 0.0, 0.0, []
    matches = []
    topic_match = 0.0
    relevance = 0.0
    for topic, key, weight in interests:
        labeled, mentioned = _topic_matches(key, candidate)
        if labeled:
            topic_match += weight
        if mentioned:
            relevance += weight
        if labeled or mentioned:
            matches.append(topic)
    return topic_match / total, relevance / total, matches


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
        if not fact.get("id") or not fact.get("article_id") or not article or not fact.get("evidence"):
            return False, "incomplete provenance"
        try:
            url = urlsplit(str(article.get("url", "")))
            linked = url.scheme in {"http", "https"} and bool(url.hostname)
        except ValueError:
            linked = False
        if not str(article.get("source") or "").strip() or not linked:
            return False, "missing publisher or source link"
        if not _quantity_supported(fact, fact.get("language") or article.get("language", "en")):
            return False, "quantity missing from cited evidence"
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
    inferred = [topic for topic, pattern in _TOPIC_PATTERNS.items() if pattern.search(blob)]
    supplied = [_topic_key(topic) for article in group for topic in article.get("topics", []) if str(topic).strip()]
    return list(dict.fromkeys(inferred + supplied)) or ["news"]


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
        exact = format(Decimal(number), "f")
    except InvalidOperation:
        return number
    return exact.rstrip("0").rstrip(".") if "." in exact else exact


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


def _comparison_context(text: str) -> set[str]:
    # Period and currency qualifiers can distinguish otherwise similar claims.
    qualifiers = re.findall(
        r"\b(?:previous|last|current|next|prior|vorjahr\w*|vergangen\w*|n[äa]chst\w*|volgend\w*|vorig\w*|"
        r"quarter\w*|quartal\w*|kwartaal\w*|year\w*|jahr\w*|jaar|q[1-4]|forecast|expected|prognose)\b",
        text.casefold(),
    )
    denominations = {"EUR": r"€|\beur(?:os?)?\b", "USD": r"\$|\b(?:usd|dollars?)\b",
                     "GBP": r"£|\b(?:gbp|pounds?)\b", "crown": r"\b(?:crowns?|kronen)\b"}
    currencies = {code for code, pattern in denominations.items() if re.search(pattern, text, re.I)}
    years = re.findall(r"\b(?:in|for|during|vor|voor|im jahr)\s+((?:19|20)\d{2})\b", text, re.I)
    return set(qualifiers + years) | currencies


def _claims_contradict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("_claim_group") == right.get("_claim_group"):
        return False
    if _canonical_unit(left.get("unit", "")) != _canonical_unit(right.get("unit", "")):
        return False
    left_text, right_text = left["statement"], right["statement"]
    if _comparison_context(left_text) != _comparison_context(right_text):
        return False
    measures = [{name for name, pattern in _MEASURE_PATTERNS.items() if pattern.search(text)}
                for text in (left_text, right_text)]
    if measures[0] and measures[1] and measures[0].isdisjoint(measures[1]):
        return False
    if left.get("language") != right.get("language"):
        # Without a common sentence vocabulary, require one explicit measure
        # and one quantity per source. Timing and denomination must also agree.
        if len(measures[0]) != 1 or measures[0] != measures[1]:
            return False
        return all(len(_NUM.findall(text)) == 1 for text in (left_text, right_text))
    if _topic_family(left_text) != _topic_family(right_text):
        return False
    first, second = _claim_terms(left.get("statement", "")), _claim_terms(right.get("statement", ""))
    shared = first & second
    # Require a specific shared metric/context. Generic words like "jobs" can
    # refer to unrelated counts within one report.
    return len(shared) >= 2 or bool(shared & {"forecast", "exports", "represented", "budget", "rate", "inflation", "receipts", "stays", "overdue", "applications", "workers"})


def _label_conflicts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep all sources for connected competing claims in one stable conflict record."""
    peers = {fact["id"]: set() for fact in facts}
    quantities = [_value_unit(f["value"], f["unit"], f["language"]) for f in facts]
    for index, fact in enumerate(facts):
        for j in range(index + 1, len(facts)):
            other = facts[j]
            if quantities[index] != quantities[j] and _claims_contradict(fact, other):
                peers[fact["id"]].add(other["id"])
                peers[other["id"]].add(fact["id"])

    conflicts = []
    conflict_by_fact = {}
    for fact_id in list(peers):
        if not peers.get(fact_id):
            continue
        group = set()
        pending = [fact_id]
        while pending:
            member = pending.pop()
            if member not in group:
                group.add(member)
                pending.extend(peers.pop(member, ()))
        ids = sorted(group)
        key = "conflict-" + hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:10]
        label = "Conflicting values reported for the same measure"
        conflict = {"id": key, "label": label, "fact_ids": ids}
        conflicts.append(conflict)
        conflict_by_fact.update(dict.fromkeys(ids, conflict))
    for fact in facts:
        fact.pop("_claim_group", None)
        if conflict := conflict_by_fact.get(fact["id"]):
            fact.update(conflict_status="conflict", conflict_group=conflict["id"], conflict_label=conflict["label"])
    return conflicts


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
        conflicts = _label_conflicts(facts)
        topic_words = _topics(group)
        member_ids = [str(a.get("article_id", "")) for a in group]
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
        story["conflicts"] = _label_conflicts(story["facts"])
    return stories or _offline_extract(articles)


def prepare_ranking(stories):
    """Index supported topic matches and provenance once per corpus snapshot."""
    candidates = []
    for story in stories:
        eligible, trust_reason = _trust_eligible(story)
        if eligible:
            topics = {_topic_key(topic) for topic in story.get("topics", [])}
            text = "\n".join([story.get("title", ""), *(fact["evidence"] for fact in story["facts"])])
            known_topics = {key: (_has_topic(key, topics), _mentions_topic(key, text)) for key in _TOPIC_PATTERNS}
            candidates.append(_RankingCandidate(story, topics, trust_reason, text, known_topics))
    return candidates


def rank(stories: list[dict[str, Any]], profile: dict[str, Any],
         *, prepared=None, limit=None) -> list[dict[str, Any]]:
    """Apply the fixed topic, relevance, recency, and novelty score."""
    excluded = {_topic_key(topic) for topic in profile.get("excluded_topics", [])} - {""}
    weights = _positive_topic_weights(profile)
    interests = sorted(
        ((topic, _topic_key(topic), weight) for topic, weight in weights.items()),
        key=lambda interest: (-interest[2], interest[0]),
    )
    total = sum(weights.values())
    corpus = prepare_ranking(stories) if prepared is None else prepared
    candidates = [
        candidate for candidate in corpus
        if not any(any(_topic_matches(key, candidate)) for key in excluded)
    ]
    eligible_stories = [candidate.story for candidate in candidates]
    ref = max((str(story.get("publication_date", ""))[:10] for story in eligible_stories), default="")
    novelty_values = _novelty_values(eligible_stories)

    def scored():
        for candidate in candidates:
            story = candidate.story
            topic_match, relevance, matches = _profile_scores(interests, total, candidate)
            recency = _recency(story, ref)
            novelty = novelty_values.get(story["id"], 0.0)
            score = 0.4 * topic_match + 0.3 * relevance + 0.2 * recency + 0.1 * novelty
            yield score, candidate, matches, topic_match, relevance, recency, novelty

    def order(row):
        story = row[1].story
        return row[0], story.get("publication_date", ""), story.get("id", "")

    top = sorted(scored(), key=order, reverse=True) if limit is None else nlargest(limit, scored(), key=order)
    rows = []
    for score, candidate, matches, topic_match, relevance, recency, novelty in top:
        story = candidate.story
        why = "Matches your interest in " + ", ".join(matches[:2]) if matches else "Sourced recent story"
        if any(f.get("conflict_status") == "conflict" for f in story.get("facts", [])):
            why += ". Conflicting figures are labeled"
        rows.append({
            **story, "trust_eligible": True, "trust_reason": candidate.trust_reason, "reason": why,
            "score_components": {
                "topic_match": round(topic_match, 4), "relevance": round(relevance, 4),
                "recency": round(recency, 4), "novelty": round(novelty, 4), "score": round(score, 4),
            },
        })
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


def _script_is_valid(candidate: str, facts: list[dict[str, Any]],
                     ordered_quantities: list[tuple[str, str]], language: str) -> bool:
    text = str(candidate or "").strip()
    without_units = re.sub(_UNIT_PATTERN, " ", text, flags=re.I)
    if not text or re.search(r"\b(?:fact|story)-[a-z0-9-]+\b", text, re.I) or _SPELLED_NUMBERS.search(without_units):
        return False
    source_hashes = {str(f.get("source_sentence_hash")) for f in facts if f.get("source_sentence_hash")}
    if any(_sentence_hash(sentence) in source_hashes for sentence in re.split(r"(?<=[.!?])\s+", text)):
        return False
    mentions = []
    for match in _NUM.finditer(text):
        number = _NUMBER.search(match.group(0))
        if number:
            unit = match.group(0)[number.end():].strip()
            mentions.append((_canonical_number(number.group(0), language), _canonical_unit(unit)))
    return list(dict.fromkeys(mentions)) == ordered_quantities


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
    """Emphasize the listener's interests while retaining the verified claims."""
    result = []
    duration = max(1, int(profile.get("duration", 5) or 5))
    limit = max(1, min(len(ranked), duration))
    output_language = str(profile.get("language", "en")).lower()
    interests = [(_topic_key(topic), weight) for topic, weight in _positive_topic_weights(profile).items()]
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
        by_quantity = {}
        for fact in preferred:
            source_language = str(fact.get("language") or by_id[str(fact["article_id"])].get("language", "en"))
            quantity = _value_unit(fact["value"], fact.get("unit", ""), source_language)
            by_quantity.setdefault(quantity, fact)

        # Sort the presentation after choosing sourced quantities and citations.
        # Stable sorting keeps the previous order when interests don't distinguish the claims.
        ordered_quantities = sorted(by_quantity, key=lambda quantity: -sum(
            weight for topic, weight in interests if _mentions_topic(topic, by_quantity[quantity]["evidence"])
        ))
        claims = []
        for quantity in ordered_quantities:
            fact = by_quantity[quantity]
            source_language = str(fact.get("language") or by_id[str(fact["article_id"])].get("language", "en"))
            value = fact["value"]
            if (source_language == "en") != (output_language == "en"):
                value = quantity[0] if output_language == "en" else quantity[0].replace(".", ",")
            amount = f"{value} {fact.get('unit', '')}".strip()
            label = _measure_label(fact, output_language, story.get("topics", []))
            claims.append((label, amount, fact["evidence"]))
        measures = "; ".join(f"{label}: {amount}" for label, amount, _ in claims)
        expertise = str(profile.get("expertise", "intermediate"))
        frames = {
            "en": {"beginner": f"In plain terms, the reports give these measures: {measures}.", "intermediate": f"The reports give these measures: {measures}.", "advanced": f"Reported measures: {measures}."},
            "de": {"beginner": f"Einfach gesagt, nennen die Berichte diese Werte: {measures}.", "intermediate": f"Die Berichte nennen diese Werte: {measures}.", "advanced": f"Berichtete Kennzahlen: {measures}."},
            "nl": {"beginner": f"Eenvoudig gezegd noemen de berichten deze cijfers: {measures}.", "intermediate": f"De berichten noemen deze cijfers: {measures}.", "advanced": f"Gerapporteerde cijfers: {measures}."},
        }
        text = frames.get(output_language, frames["en"]).get(expertise, frames.get(output_language, frames["en"])["intermediate"])
        conflict_text = {"en": "The reports give conflicting values.", "de": "Die Berichte nennen unterschiedliche Werte.", "nl": "De berichten noemen verschillende waarden."}.get(output_language, "The reports give conflicting values.")
        # Use the local summary when a provider drops, adds, or reorders quantities.
        if getattr(providers, "live", False):
            context_rows = "\n".join(
                f"source context={evidence}; verified amount={amount}"
                for _, amount, evidence in claims
            )
            prompt = (
                f"Write a concise paraphrase in language {output_language}. Lead with the first claim, "
                "then cover every verified amount and unit in the given order. Keep each quantity attached "
                "to its reported measure. Use only the evidence below. Add no numbers, including number words, "
                "and do not copy full article sentences. "
                f"Listener expertise: {expertise}; style: {profile.get('style', 'neutral')}\n"
                f"{context_rows}\nConflicting reports: {conflicted}"
            )
            candidate = providers.generate_script(prompt, output_language)
            if _script_is_valid(candidate, facts, ordered_quantities, output_language):
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
