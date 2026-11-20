"""Deterministic news preparation, ranking, and factual narration."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from heapq import nlargest
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit

from .clustering import cosine, group_articles, merge_same_event
from .providers import RECOVERABLE_EXTRACTION_ERRORS

_LOG = logging.getLogger(__name__)


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

# Plain terms for ranking relevance. A story matches a topic axis in proportion
# to how many of these terms appear in its title and verified facts.
_RELEVANCE_TERMS = {
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
    "earnings": ("earnings", "profit", "revenue", "winst", "gewinn", "umsatz", "omzet"),
    "technology": ("technology", "technologie", "software", "semiconductor"),
}


_TOPIC_ALIASES = {"rate": "rates", "interest rate": "rates", "interest rates": "rates", "trade and exports": "trade", "prices": "inflation", "employment": "jobs", "transportation": "transport"}
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


def _topic_terms(value: str) -> tuple[str, ...]:
    key = _topic_key(value)
    terms = _RELEVANCE_TERMS.get(key)
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
        if not fact.get("id") or not fact.get("article_id") or not article or not fact.get("evidence"):
            return False, "incomplete provenance"
        try:
            url = urlsplit(str(article.get("url", "")))
            linked = url.scheme in {"http", "https"} and bool(url.hostname)
        except ValueError:
            linked = False
        if not str(article.get("source") or "").strip() or not linked:
            return False, "missing publisher or source link"
        sources.add(str(article["article_id"]))
    # Unverifiable figures are dropped at narration; a story needs at least one verified figure to be spoken.
    if not any(
        _quantity_supported(fact, fact.get("language") or articles[str(fact["article_id"])].get("language", "en"))
        for fact in facts
    ):
        return False, "quantity missing from cited evidence"
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


def _is_about(pattern: re.Pattern[str], article: dict[str, Any]) -> bool:
    """A topic names what an article is about: in its headline, or repeated in its text.

    One passing mention ("conditional on energy prices") does not make a central bank story an
    energy story. Each article is judged alone, so translations of one mention do not add up.
    """
    return bool(pattern.search(str(article.get("title", "")))) or len(pattern.findall(str(article.get("body", ""))[:2500])) >= 2


def _topics(group: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Topics of a story, and the inferred ones that are only mentioned in passing."""
    inferred, passing = [], []
    for topic, pattern in _TOPIC_PATTERNS.items():
        mentioning = [a for a in group if pattern.search(str(a.get("title", "")) + " " + str(a.get("body", ""))[:2500])]
        if mentioning:
            inferred.append(topic)
            if not any(_is_about(pattern, a) for a in mentioning):
                passing.append(topic)
    supplied = [_topic_key(topic) for article in group for topic in article.get("topics", []) if str(topic).strip()]
    # A topic the publisher supplied is never a passing mention.
    return list(dict.fromkeys(inferred + supplied)) or ["news"], [topic for topic in passing if topic not in supplied]


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


def _decimal_text(token: str, language: str = "en") -> str:
    """Digits with a "." decimal point and no grouping, keeping trailing zeros.

    German and Dutch write "1.234,5", but their texts also carry English-style decimals ("3.31 Milliarden").
    A "." is a thousands separator only when every group after it has exactly three digits.
    """
    number = token.replace("−", "-")
    if language in {"de", "nl"}:
        if "," in number:
            number = number.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", number):
            number = number.replace(".", "")
    else:
        number = number.replace(",", "")
    return number


def _canonical_number(value: str, language: str = "en") -> str:
    token = _NUMBER.search(str(value))
    if not token:
        return ""
    number = _decimal_text(token.group(0), language)
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


def _locate_evidence(body: str, evidence: str, value: str) -> tuple[int, str]:
    """Find a model's quote in the article and return (offset, the article's own text), or (-1, "").

    Models often capitalise the first word or re-punctuate and paraphrase the tail of a quote.
    Match case-insensitively and trim the tail word by word, never past the quoted figure, then
    return an equally long span of the article itself, so stored evidence is always exact source text.
    """
    evidence = evidence.strip()
    if not body or not evidence:
        return -1, ""
    at = body.find(evidence)
    if at >= 0:
        return at, evidence
    number = _NUMBER.search(str(value).lstrip("-−"))
    figure_end = evidence.find(number.group(0)) + len(number.group(0)) if number and number.group(0) in evidence else -1
    if figure_end < 0:
        return -1, ""
    lowered = body.lower()
    candidate = evidence.rstrip(".,;:!? ")
    while len(candidate) >= max(figure_end, 24):
        at = lowered.find(candidate.lower())
        if at >= 0:
            return at, body[at:at + len(evidence)]
        if " " not in candidate[figure_end:]:
            break
        candidate = candidate.rsplit(" ", 1)[0].rstrip(".,;:!? ")
    return -1, ""


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


# Within one article, two figures for a measure are a range, a scenario or a before and after, unless
# the article itself sets one apart as another source's number ("a separate estimate from ...").
_ALTERNATIVE_SOURCE = re.compile(
    r"\b(?:separate|another|alternative|competing|independent|second)\s+(?:\w+\s+)?"
    r"(?:estimates?|figures?|counts?|tall(?:y|ies)|calculations?|forecasts?|projections?|surveys?)\b"
    r"|\b(?:separate|weitere|andere|alternative)\s+(?:schätzung|zahl|zählung|prognose|berechnung)\w*"
    r"|\b(?:aparte|afzonderlijke|andere|alternatieve)\s+(?:schatting|cijfers?|telling|raming|prognose)\w*",
    re.I,
)


def _label_conflicts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep all sources for connected competing claims in one stable conflict record."""
    peers = {fact["id"]: set() for fact in facts}
    quantities = [_value_unit(f["value"], f["unit"], f["language"]) for f in facts]
    for index, fact in enumerate(facts):
        for j in range(index + 1, len(facts)):
            other = facts[j]
            if fact.get("article_id") == other.get("article_id") and not (
                _ALTERNATIVE_SOURCE.search(str(fact.get("statement", ""))) or _ALTERNATIVE_SOURCE.search(str(other.get("statement", "")))
            ):
                continue
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
        conflicts = _label_conflicts(facts)
        topic_words, passing_topics = _topics(group)
        member_ids = [str(a.get("article_id", "")) for a in group]
        stories.append({"id": _story_id(member_ids, str(lead.get("title", "")), _day(lead)), "title": str(lead.get("title", "Untitled")), "articles": all_articles,
                        "facts": facts, "topics": topic_words or ["news"], "publication_date": _day(lead), "conflicts": conflicts,
                        **({"passing_topics": passing_topics} if passing_topics else {})})
    return stories


def _extract_batch(providers: Any, batch: list[dict[str, Any]], heuristic_ids: set[str]) -> list[dict[str, Any]]:
    """Extract one batch; a truncated or invalid reply is retried as two halves, down to single articles.

    A single article that still fails is added to ``heuristic_ids`` for local extraction. Any other
    provider error (authentication, connection, quota) propagates unchanged and aborts the run.
    """
    try:
        return (providers.extract(batch) or {}).get("stories", [])
    except RECOVERABLE_EXTRACTION_ERRORS as exc:
        if len(batch) == 1:
            _LOG.warning("Live extraction reply unusable for 1 article (%s); using the local heuristic for it",
                         type(exc).__name__)
            heuristic_ids.add(str(batch[0].get("article_id", "")))
            return []
        middle = len(batch) // 2
        _LOG.warning("Live extraction reply unusable for a batch of %d articles (%s); retrying as %d + %d",
                     len(batch), type(exc).__name__, middle, len(batch) - middle)
        return _extract_batch(providers, batch[:middle], heuristic_ids) + _extract_batch(providers, batch[middle:], heuristic_ids)


def _note_heuristic_articles(providers: Any, count: int) -> None:
    """Let the import report how many articles used the local heuristic instead of the provider."""
    if getattr(providers, "live", False):
        try:
            providers.heuristic_fallback_articles = count
        except AttributeError:
            pass


def prepare(articles: list[dict[str, Any]], providers: Any) -> list[dict[str, Any]]:
    """Convert article records to story records while preserving source links."""
    if not articles:
        return []
    extracted: dict[str, Any] = {}
    heuristic_ids: set[str] = set()
    vectors_by_article: dict[str, list[float]] = {}
    if getattr(providers, "live", False):
        groups, vectors_by_article = _merge_same_event(group_articles(articles, _group_key, _day), providers)
        extracted_stories = []
        try:
            for batch in _provider_batches(groups):
                extracted_stories.extend(_extract_batch(providers, batch, heuristic_ids))
        except Exception:
            # Keep the corpus usable if an opted-in provider call fails midway;
            # local extraction still enforces evidence and numeric provenance.
            _note_heuristic_articles(providers, len(articles))
            return _offline_extract(articles)
        extracted = {"stories": extracted_stories}
    if not extracted.get("stories"):
        _note_heuristic_articles(providers, len(articles))
        return _offline_extract(articles)
    _note_heuristic_articles(providers, len(heuristic_ids))
    # A live model proposes clusters, but only exact source evidence and
    # quantities found in that article are allowed into the prepared corpus.
    by_id = {str(a.get("article_id")): a for a in articles}
    members_by_article = {}
    for group in groups:
        # Articles that fell back to the heuristic become their own stories below.
        member_ids = {str(article.get("article_id", "")) for article in group} - heuristic_ids
        for article_id in member_ids:
            members_by_article[article_id] = member_ids
    clustered: dict[str, dict[str, Any]] = {}
    for item in extracted["stories"]:
        facts: list[dict[str, Any]] = []
        for raw in item.get("facts", []):
            aid = str(raw.get("article_id", "")); article = by_id.get(aid)
            body = str(article.get("body", "")) if article else ""
            value = str(raw.get("value", "")).strip()
            evidence_offset, evidence = _locate_evidence(body, str(raw.get("evidence", "")), value)
            if not article or not evidence or evidence_offset < 0:
                continue
            language = _language(article)
            if value[:1] in "-−" and value not in evidence:
                # "fell 0.3%" is quoted without a sign; keep the figure as the article prints it.
                value = value[1:]
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
        story["conflicts"] = _label_conflicts(story["facts"])
    if not stories:
        _note_heuristic_articles(providers, len(articles))
        return _offline_extract(articles)
    if heuristic_ids:
        # Same local extraction and provenance rules as the offline path, for just these articles.
        known = {story["id"] for story in stories}
        stories.extend(story for story in _offline_extract([a for a in articles if str(a.get("article_id")) in heuristic_ids])
                       if story["id"] not in known)
    for story in stories:
        # Kept with the story so briefings can avoid playing near-identical stories back to back.
        vector = next((vectors_by_article[a["article_id"]] for a in story["articles"] if a["article_id"] in vectors_by_article), None)
        if vector:
            story["embedding"] = vector
    return stories


EMBEDDING_DIMENSIONS = 256


def _merge_same_event(groups: list[list[dict[str, Any]]], providers: Any) -> tuple[list[list[dict[str, Any]]], dict[str, list[float]]]:
    """Embed each group's lead article once, merge cross-outlet reports of one event, keep the vectors.

    Embedding is an aid, not a requirement: if it fails or returns an unexpected shape, the groups are
    used as they are. Extraction afterwards still decides whether the provider is usable at all.
    """
    leads = [next((a for a in group if _language(a) == "en"), group[0]) for group in groups]
    texts = [(str(a.get("title", "")) + "\n" + str(a.get("body", ""))[:1200]).strip() for a in leads]
    try:
        vectors = providers.embed(texts, stage="ingestion", dimensions=EMBEDDING_DIMENSIONS)
        if not isinstance(vectors, list) or len(vectors) != len(groups):
            raise ValueError("unexpected embedding response")
        vectors = [[round(float(x), 5) for x in vector] for vector in vectors]
    except Exception as exc:
        _LOG.info("Story embeddings unavailable (%s); grouping without them", type(exc).__name__)
        return groups, {}
    by_group = {id(group): vector for group, vector in zip(groups, vectors)}
    merged = merge_same_event(groups, vectors)
    if len(merged) < len(groups):
        _LOG.info("Merged %d cross-outlet reports of the same event", len(groups) - len(merged))
    vectors_by_article = {}
    for group in groups:
        for article in group:
            vectors_by_article[str(article.get("article_id", ""))] = by_group[id(group)]
    return merged, vectors_by_article


DIVERSITY_SIMILARITY = 0.80


def diversify(stories: list[dict[str, Any]], threshold: float = DIVERSITY_SIMILARITY) -> list[dict[str, Any]]:
    """Keep ranking order but pass over a story that is a near-twin of one already chosen.

    Stories without embeddings (offline mode) are never passed over.
    """
    kept: list[dict[str, Any]] = []
    for story in stories:
        vector = story.get("embedding")
        if vector and any(cosine(vector, other["embedding"]) >= threshold for other in kept if other.get("embedding")):
            continue
        kept.append(story)
    return kept


def _profile_names(profile: dict[str, Any]) -> dict[str, str]:
    """Listener-facing names for weighted axes, so reasons repeat the listener's own words."""
    names: dict[str, str] = {}
    for topic in profile.get("topic_weights") or {}:
        names.setdefault(_topic_key(topic), str(topic))
    return names


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
    firm_topics: set[str]  # matching topics the story is about, not just mentions in passing
    relevance: dict[str, float]
    mentions: frozenset[str]
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
    # Unsourced or unlinked stories must not move the recency reference or duplicate counts.
    # Provenance is judged on all original facts. Memory then drops what was already said.
    sourced = [story for story in stories if _trust_eligible(story)[0]]
    if spoken:
        sourced = [story for story in (_unspoken_story(story, spoken) for story in sourced) if _trust_eligible(story)[0]]
    reference_day = max((str(story.get("publication_date", ""))[:10] for story in sourced), default="")
    novelty = _novelty_values(sourced)
    vocabulary = {
        axis: tuple(re.compile(rf"(?<![\w]){re.escape(term)}[\w-]*(?![\w])") for term in _topic_terms(axis))
        for axis in sorted(weight_axes)
    }
    vocabulary = {axis: patterns for axis, patterns in vocabulary.items() if patterns}
    axes = weight_axes | exclusion_axes
    rows = []
    for story in sourced:
        if not _matches_region(story, region):
            continue
        _, trust_reason = _trust_eligible(story)
        topics = _story_topics(story)
        content = " ".join(
            [str(story.get("title", ""))]
            + [str(fact.get("statement", "")) for fact in story.get("facts", [])]
        ).lower()
        evidence = " ".join([str(story.get("title", "")), *(str(fact.get("evidence", "")) for fact in story.get("facts", []))])
        labeled = frozenset(axis for axis in exclusion_axes if _has_topic(axis, topics))
        mentions = frozenset(axis for axis in axes if _mentions_topic(axis, evidence)) | labeled
        relevance = {
            axis: sum(bool(pattern.search(content)) for pattern in patterns) / len(patterns)
            for axis, patterns in vocabulary.items()
        }
        about = topics - {_topic_key(topic) for topic in story.get("passing_topics", ())}
        rows.append(_StoryFeatures(
            story, {axis for axis in axes if _matches_topic(axis, topics)},
            {axis for axis in axes if _matches_topic(axis, about)}, relevance, mentions,
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
        _rank_profile(features, row, excluded, relevance_axes, _profile_names(profile))
        for row, excluded, profile in zip(weights, exclusions, profiles)
    ]


def _rank_profile(
    features: list[_StoryFeatures],
    weights: list[tuple[str, float]],
    excluded: set[str],
    relevance_axes: set[str],
    names: dict[str, str],
) -> list[dict[str, Any]]:
    topic_total = sum(weight for _, weight in weights)
    relevance_weights = [(axis, weight) for axis, weight in weights if axis in relevance_axes]
    relevance_total = 0.0
    for _, weight in relevance_weights:
        relevance_total += weight
    rows = []
    for feature in features:
        if excluded & (feature.matching_topics | feature.mentions):
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
        # The reason claims a match only for topics the story is about. A passing mention, or words in
        # its figures that merely touch the interest, is reported as a mention.
        firm = [(axis, weight) for axis, weight in matching if axis in feature.firm_topics]
        related = firm or matching or [
            (axis, feature.relevance[axis] * weight) for axis, weight in relevance_weights
            if feature.relevance[axis] and weight > 0
        ]
        if related:
            contributions: dict[str, float] = {}
            for axis, amount in related:
                contributions[axis] = contributions.get(axis, 0.0) + amount
            topics = sorted(contributions, key=lambda axis: (-contributions[axis], axis))[:3]
            why = ("Matches your interest in " if firm else "Mentions ") + ", ".join(names.get(axis, axis) for axis in topics)
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


def prepare_ranking(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Corpus snapshot handed to rank(prepared=...). Ranking derives its features per call."""
    return list(stories)


def rank(
    stories: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    feedback_events: Iterable[dict[str, Any]] | None = None,
    spoken_fact_ids: Iterable[str] | None = None,
    region: str = "all",
    prepared: list[dict[str, Any]] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank with listener-scoped events and IDs from the preceding seven days.

    The caller owns listener identity and expiration. Feedback must be in
    chronological order or already reduced to the latest rating per story.
    """
    region = region.strip().lower()
    if region not in {"all", "europe"} and not re.fullmatch(r"[a-z]{2}", region):
        raise ValueError("Region must be all, Europe, or an ISO alpha-2 country code")
    ranked = _rank_batch(
        stories if prepared is None else prepared, [profile], feedback_events or (), set(spoken_fact_ids or ()), region,
    )[0]
    return ranked if limit is None else ranked[:limit]


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
    # Listener-facing names for what a figure measures. Specific phrases come before general ones:
    # when two phrases sit equally close to a figure, the earlier entry names it.
    (r"jobs?\s+(?:were\s+)?represented|represented\s+jobs?|vertretene\s+arbeitsplätze|vertegenwoordigde\s+banen",
     {"en": "jobs represented", "de": "vertretene Arbeitsplätze", "nl": "vertegenwoordigde banen"}),
    (r"revenue|turnover|umsatz|omzet", {"en": "revenue", "de": "Umsatz", "nl": "omzet"}),
    (r"(?:operating )?profit|gewinn|winst", {"en": "profit", "de": "Gewinn", "nl": "winst"}),
    (r"growth forecast|wachstumsprognose|groeiverwachting", {"en": "growth forecast", "de": "Wachstumsprognose", "nl": "groeiverwachting"}),
    (r"policy rate|leitzins|beleidsrente", {"en": "policy rate", "de": "Leitzins", "nl": "beleidsrente"}),
    (r"basis points?|basispunkte|basispunten", {"en": "rate change", "de": "Zinsänderung", "nl": "renteverandering"}),
    (r"inflati(?:on|e)|teuerung", {"en": "inflation", "de": "Inflation", "nl": "inflatie"}),
    (r"pay rise|wage|lohn|loon", {"en": "pay increase", "de": "Lohnerhöhung", "nl": "loonsverhoging"}),
    (r"jobs?|arbeitsplätze|banen", {"en": "jobs", "de": "Arbeitsplätze", "nl": "banen"}),
    (r"workers?|arbeitnehmer|werknemers", {"en": "workers", "de": "Arbeitnehmer", "nl": "werknemers"}),
    (r"applications?|anträge|aanvragen", {"en": "applications", "de": "Anträge", "nl": "aanvragen"}),
    (r"requesting|requested|beantragt\w*|aangevraagd", {"en": "amount requested", "de": "beantragter Betrag", "nl": "aangevraagd bedrag"}),
    (r"budget", {"en": "budget", "de": "Budget", "nl": "budget"}),
    (r"saving rate|savings|sparquote|spaarquote", {"en": "saving rate", "de": "Sparquote", "nl": "spaarquote"}),
    (r"food prices|lebensmittelpreise|voedselprijzen", {"en": "food prices", "de": "Lebensmittelpreise", "nl": "voedselprijzen"}),
    (r"solar capacity|solarkapazität|photovoltaikkapazität|zonnecapaciteit", {"en": "solar capacity", "de": "Solarkapazität", "nl": "zonnecapaciteit"}),
    (r"per megawatt|weighted average price|contract bid", {"en": "contract price", "de": "Vertragspreis", "nl": "contractprijs"}),
    (r"tourism receipts|tourismus|toerisme|touristische einnahmen", {"en": "tourism receipts", "de": "Tourismuseinnahmen", "nl": "toeristische inkomsten"}),
    (r"overnight stays|nächtigungen|overnachtingen", {"en": "overnight stays", "de": "Übernachtungen", "nl": "overnachtingen"}),
    (r"credit line|kreditlinie|kredietlijn", {"en": "credit line", "de": "Kreditlinie", "nl": "kredietlijn"}),
    (r"loans?\s+(?:will be\s+|are\s+)?capped at|loan cap", {"en": "loan cap", "de": "Kreditobergrenze", "nl": "kredietplafond"}),
    (r"capped at|cap of|ceiling of|obergrenze|plafond", {"en": "cap", "de": "Obergrenze", "nl": "plafond"}),
    (r"farm loans?(?=[^.;]*(?:overdue|late))|agrarkredite?(?=[^.;]*überfällig)|landbouwleningen(?=[^.;]*achterstallig)",
     {"en": "overdue farm loans", "de": "überfällige Agrarkredite", "nl": "achterstallige landbouwleningen"}),
    (r"overdue|überfällig|achterstallig", {"en": "overdue threshold", "de": "Überfälligkeitsgrenze", "nl": "achterstandsgrens"}),
    (r"loans?|kredite|leningen", {"en": "loans", "de": "Kredite", "nl": "leningen"}),
    (r"waiting time|wartezeit|wachttijd", {"en": "waiting time", "de": "Wartezeit", "nl": "wachttijd"}),
)
# A figure is described by the closest phrase in its own sentence. Punctuation that ends a clause,
# or a long stretch of unrelated words, means the phrase belongs to something else.
_LABEL_BOUNDARY = re.compile(
    r"[,;:()–—]|[.!?](?:\s|$)|\s-\s"
    r"|\b(?:while|whilst|whereas|but|although|though|however|which|aber|während|obwohl|maar|terwijl|hoewel)\b",
    re.I,
)
_MAX_LABEL_GAP_WORDS = 8
_PREVIOUS_BEFORE = re.compile(r"\b(?:from|compared (?:with|to)|versus|gegenüber|vergeleken met|ten opzichte van)\s+$")
_RANGE_FROM_BEFORE = re.compile(r"\b(?:von|van)\s+$")
_RANGE_TO_AFTER = re.compile(r"\s*(?:auf|bis|naar|tot|to)\s+[−€\d-]")
_ALSO_BEFORE = re.compile(r"\b(?:and|und|en|or|oder)\s+$")
_RANGE_JOIN = re.compile(r"\s*(?:to|and|or|auf|bis|und|oder|naar|tot|en)\s+(?=[−€\d-])")
# The figure pattern cuts "Millionen" short at "million", so an "en" may remain before the currency.
_CURRENCY_AFTER = re.compile(r"(?:en)?\s+(?:of\s+)?(crowns?|kronen|euros?|euro|dollars?|pounds?)\b")
_CURRENCY_NAMES = {
    "crown": "crown", "crowns": "crown", "kronen": "crown", "euro": "euro", "euros": "euro",
    "dollar": "dollar", "dollars": "dollar", "pound": "pound", "pounds": "pound",
}
_CURRENCY_SYMBOLS = {"$": "dollar", "£": "pound", "€": "euro"}
# A range ("2-3%", "between 2 and 3 percent") is one claim, not two figures.
_RANGE_DASH = re.compile(r"\s*(?:[-–—]|to|bis|tot)\s*[$£€]?\s*")
_RANGE_AND = re.compile(r"\s*(?:and|und|en)\s*[$£€]?\s*")
_BETWEEN_BEFORE = re.compile(r"\b(?:between|zwischen|tussen)\s+[$£€]?\s*$")
# The figure pattern does not know every scale or multiple. A bare number followed by one ("165k", "$1.5T",
# "18.5x", "25 bps") would be spoken a thousand times too small or without its meaning, so it is not spoken.
_UNRECOGNISED_SUFFIX = re.compile(
    r"[a-zA-Z×¢]|\s+(?:k|m|b|t|bn|mn|tn|trn|mio|mrd|mln|mld|x|trillion|thousand|hundred|bps?|bips|cents?|pence|times|fold|pct)\b",
    re.I,
)
_PERCENTAGE_POINTS = re.compile(r"age\s+points?\b")
# Measures that are themselves percentages. A percentage beside "revenue" or "profit" is a change or a ratio.
_PERCENT_MEASURES = frozenset({
    "growth forecast", "policy rate", "inflation", "pay increase", "saving rate", "food prices", "overdue farm loans",
})
_PER_HOUR_AFTER = re.compile(r"\s+per\s+megawatt[- ]hour\b")
_PER_HOUR = " per megawatt-hour"
_PREVIOUS_LABEL = {"en": "previous {}", "de": "{} zuvor", "nl": "{} eerder"}


class _Amount(NamedTuple):
    start: int
    end: int
    quantity: tuple[str, str]  # canonical number and the unit as written, which may be empty
    previous: bool  # the earlier value of a change: "from 1.8%", "von 1,8 auf 1,4 Prozent"
    usable: bool  # False for a range endpoint, or a number whose scale or multiple is not understood


class _Figure(NamedTuple):
    label: str | None  # None when the evidence names no measure for this figure
    unit: str  # canonical unit, completed from the evidence when the figure leaves it out
    slot: tuple[int, bool] | None  # language-independent identity of the label: catalogue entry, previous value
    speakable: bool  # False when it cannot be said faithfully, so it is left out of the narration


def _evidence_amounts(text: str, language: str) -> list[_Amount]:
    amounts = []
    for match in _NUM.finditer(text):
        number = _NUMBER.search(match.group(0))
        if not number:
            continue
        raw = number.group(0)
        unit = match.group(0)[number.end():].strip()
        # Calendar years name a period, not an amount.
        if raw.isdigit() and len(raw) == 4 and 1900 <= int(raw) <= 2100 and not unit:
            continue
        before, after = text[:match.start()], text[match.end():]
        previous = bool(_PREVIOUS_BEFORE.search(before)) or bool(_RANGE_FROM_BEFORE.search(before) and _RANGE_TO_AFTER.match(after))
        quantity = (_canonical_number(raw, language), _canonical_unit(unit))
        misunderstood = (not quantity[1] and bool(_UNRECOGNISED_SUFFIX.match(after))) or (
            quantity[1] == "percent" and bool(_PERCENTAGE_POINTS.match(after))
        )
        amounts.append(_Amount(match.start(), match.end(), quantity, previous, not misunderstood))
    for index in range(len(amounts) - 1):
        first, second = amounts[index], amounts[index + 1]
        between = text[first.end:second.start]
        # "2 to 3 percent" is a range; "rose 5% to 4.2 billion" is not, because the units differ.
        if not first.previous and first.quantity[1] in {"", second.quantity[1]} and (
            _RANGE_DASH.fullmatch(between)
            or (_RANGE_AND.fullmatch(between) and _BETWEEN_BEFORE.search(text[:first.start]))
        ):
            amounts[index], amounts[index + 1] = first._replace(usable=False), second._replace(usable=False)
    return amounts


def _complete_unit(text: str, amount: _Amount) -> str:
    """Canonical unit of an amount, filling in what the sentence says elsewhere.

    "von 1,8 auf 1,4 Prozent" shares one unit, "2.3 billion crowns" names its currency after the
    scale word and "48 crowns per megawatt-hour" its rate. Nothing is added that the text lacks.
    """
    unit, end = amount.quantity[1], amount.end
    # "$3.31 billion", "£5", "€ 40 million": the symbol names the currency.
    symbol = text[amount.start] if text[amount.start:amount.start + 1] == "€" else text[:amount.start].rstrip()[-1:]
    symbol_currency = _CURRENCY_SYMBOLS.get(symbol, "")
    if not unit:
        joined = _RANGE_JOIN.match(text, end)
        following = _NUM.match(text, joined.end()) if joined else None
        number = _NUMBER.search(following.group(0)) if following else None
        shared = _canonical_unit(following.group(0)[number.end():].strip()) if number else ""
        return shared or symbol_currency
    if unit in {"million", "billion"}:
        if currency := _CURRENCY_AFTER.match(text, end):
            unit, end = f"{unit} {_CURRENCY_NAMES[currency.group(1)]}", currency.end()
        elif symbol_currency:
            unit = f"{unit} {symbol_currency}"
    if unit.rsplit(" ", 1)[-1] in set(_CURRENCY_NAMES.values()) and _PER_HOUR_AFTER.match(text, end):
        unit += _PER_HOUR
    return unit


def _label_gap(text: str, span: tuple[int, int], amount: _Amount) -> str | None:
    """The words between a phrase and a figure, or None if they cannot belong together."""
    if span[1] <= amount.start:
        gap = text[span[1]:amount.start]
    elif amount.end <= span[0]:
        gap = text[amount.end:span[0]]
    else:
        gap = ""
    # A figure standing between them ("von 1,8 auf") is a word, not punctuation.
    gap = _NUM.sub(" # ", gap)
    if _LABEL_BOUNDARY.search(gap) or len(gap.split()) > _MAX_LABEL_GAP_WORDS:
        return None
    return gap


def _hyphenated(text: str, span: tuple[int, int]) -> bool:
    """A phrase joined to another word by a hyphen is part of a compound: "Kurs-Gewinn", "cost-to-revenue"."""
    start, end = span
    return (start >= 2 and text[start - 1] == "-" and text[start - 2].isalnum()) or (
        text[end:end + 1] == "-" and text[end + 1:end + 2].isalnum()
    )


def _owned_labels(text: str, amounts: list[_Amount]) -> dict[_Amount, int]:
    """Catalogue entry naming each figure. A phrase names only the figure closest to it."""
    firm = [amount for amount in amounts if amount.usable and not amount.previous]
    best: dict[_Amount, tuple[int, int]] = {}
    for index, (pattern, labels) in enumerate(_MEASURE_LABELS):
        candidates = [a for a in firm if a.quantity[1] != "percent" or labels["en"] in _PERCENT_MEASURES]
        if not candidates:
            continue
        for match in re.finditer(pattern, text):
            span = match.span()
            if _hyphenated(text, span):
                continue
            # The closest figure it can be bound to. Equally close figures: a phrase leading into a figure
            # ("requesting 96 million") names the later one.
            options = [
                ((max(0, span[0] - a.end, a.start - span[1]), a.start < span[1]), a, gap)
                for a in candidates if (gap := _label_gap(text, span, a)) is not None
            ]
            if not options:
                continue
            _, owner, gap = min(options, key=lambda option: option[0])
            if owner not in best or (len(gap), index) < best[owner]:
                best[owner] = (len(gap), index)
    return {amount: found[1] for amount, found in best.items()}


def _describe_figure(fact: dict[str, Any], source_language: str, language: str) -> _Figure:
    return _describe_evidence(
        str(fact.get("evidence", "")), str(fact.get("value", "")), str(fact.get("unit", "")), source_language, language,
    )


@lru_cache(maxsize=8192)
def _describe_evidence(evidence: str, value: str, unit: str, source_language: str, language: str) -> _Figure:
    """Name what a verified figure measures using only the words of its cited evidence."""
    text = evidence.lower()
    expected = _value_unit(value, unit, source_language)
    amounts = _evidence_amounts(text, source_language)
    target = next((amount for amount in amounts if amount.quantity == expected), None)
    if target is None:
        return _Figure(None, expected[1], None, False)
    unit = _complete_unit(text, target)
    if not target.usable:
        return _Figure(None, unit, None, False)
    owned = _owned_labels(text, amounts)
    index = owned.get(target)
    previous = target.previous
    if previous:
        # The earlier value of a change is the same measure as the figure it is compared with.
        others = [a for a in amounts if not a.previous and a in owned and _complete_unit(text, a) == unit]
        if others:
            nearest = min(others, key=lambda a: max(0, target.start - a.end, a.start - target.end))
            index = owned[nearest]
    elif index is None and _ALSO_BEFORE.search(text[:target.start]):
        # "a 4.2% pay rise in the first year and 3.0% in the second": the same measure again.
        earlier = [a for a in amounts if a.end <= target.start and a in owned and _complete_unit(text, a) == unit]
        if earlier and not re.search(r"[.;]", text[earlier[-1].end:target.start]):
            index = owned[earlier[-1]]
    if index is None:
        # Unnamed, but a unit says what it counts. An earlier value on its own would mislead.
        return _Figure(None, unit, None, bool(unit) and not previous)
    labels = _MEASURE_LABELS[index][1]
    label = labels.get(language, labels["en"])
    if previous:
        label = _PREVIOUS_LABEL.get(language, _PREVIOUS_LABEL["en"]).format(label)
    return _Figure(label, unit, (index, previous), True)


def _spoken_number(value: str, source_language: str, output_language: str) -> str:
    token = _NUMBER.search(value).group(0)
    source_comma, output_comma = source_language in {"de", "nl"}, output_language in {"de", "nl"}
    plain = _decimal_text(token, source_language)
    # An English-style decimal inside German or Dutch text ("3.31") still has to be written with a comma there.
    english_decimal = source_comma and "," not in token and "." in plain
    if source_comma == output_comma and not english_decimal:
        return token
    return plain.replace(".", ",") if output_comma else plain


# Spoken forms of the units figures are reported in: (surface forms, {language: (singular, plural)}).
_UNIT_NOUNS = (
    ({"percent"}, {"en": ("percent", "percent"), "de": ("Prozent", "Prozent"), "nl": ("procent", "procent")}),
    ({"basis points", "basis point", "basispunkte", "basispunkt", "basispunten", "basispunt"},
     {"en": ("basis point", "basis points"), "de": ("Basispunkt", "Basispunkte"), "nl": ("basispunt", "basispunten")}),
    ({"applications", "application", "anträge", "antrag", "aanvragen", "aanvraag"},
     {"en": ("application", "applications"), "de": ("Antrag", "Anträge"), "nl": ("aanvraag", "aanvragen")}),
    ({"workers", "worker", "arbeitnehmer", "werknemers", "werknemer"},
     {"en": ("worker", "workers"), "de": ("Arbeitnehmer", "Arbeitnehmer"), "nl": ("werknemer", "werknemers")}),
    ({"jobs", "job", "arbeitsplätze", "arbeitsplatz", "banen", "baan"},
     {"en": ("job", "jobs"), "de": ("Arbeitsplatz", "Arbeitsplätze"), "nl": ("baan", "banen")}),
    ({"days", "day", "tage", "tag", "dagen", "dag"}, {"en": ("day", "days"), "de": ("Tag", "Tage"), "nl": ("dag", "dagen")}),
    ({"hours", "hour", "stunden", "stunde", "uren", "uur"}, {"en": ("hour", "hours"), "de": ("Stunde", "Stunden"), "nl": ("uur", "uren")}),
    ({"years", "year", "jahre", "jahr", "jaren", "jaar"}, {"en": ("year", "years"), "de": ("Jahr", "Jahre"), "nl": ("jaar", "jaren")}),
    ({"meetings", "meeting", "sitzungen", "sitzung", "vergaderingen", "vergadering"},
     {"en": ("meeting", "meetings"), "de": ("Sitzung", "Sitzungen"), "nl": ("vergadering", "vergaderingen")}),
    ({"megawatts", "megawatt"}, {"en": ("megawatt", "megawatts"), "de": ("Megawatt", "Megawatt"), "nl": ("megawatt", "megawatt")}),
    ({"megawatt-hours", "megawatt-hour"},
     {"en": ("megawatt-hour", "megawatt-hours"), "de": ("Megawattstunde", "Megawattstunden"), "nl": ("megawattuur", "megawattuur")}),
    ({"crown", "crowns", "kronen", "krone", "kroon"}, {"en": ("crown", "crowns"), "de": ("Krone", "Kronen"), "nl": ("kroon", "kronen")}),
    ({"euro", "euros", "eur"}, {"en": ("euro", "euros"), "de": ("Euro", "Euro"), "nl": ("euro", "euro")}),
    ({"dollar", "dollars"}, {"en": ("dollar", "dollars"), "de": ("Dollar", "Dollar"), "nl": ("dollar", "dollar")}),
    ({"pound", "pounds"}, {"en": ("pound", "pounds"), "de": ("Pfund", "Pfund"), "nl": ("pond", "pond")}),
)
_UNIT_SCALES = (
    ({"million", "millionen", "miljoen", "mn"}, {"en": ("million", "million"), "de": ("Million", "Millionen"), "nl": ("miljoen", "miljoen")}),
    ({"billion", "milliarden", "miljard", "bn"}, {"en": ("billion", "billion"), "de": ("Milliarde", "Milliarden"), "nl": ("miljard", "miljard")}),
)
_PER_HOUR_SPOKEN = {"en": _PER_HOUR, "de": " pro Megawattstunde", "nl": " per megawattuur"}


def _parse_unit(unit: str) -> tuple[int | None, int | None, str] | None:
    """Scale word, noun and rate of a unit, or None for an unfamiliar one."""
    suffix = ""
    if unit.endswith(_PER_HOUR):
        unit, suffix = unit[:-len(_PER_HOUR)], _PER_HOUR
    words = unit.split()
    scale = next((i for i, (aliases, _) in enumerate(_UNIT_SCALES) if words and words[0] in aliases), None)
    if scale is not None:
        words = words[1:]
    noun = next((i for i, (aliases, _) in enumerate(_UNIT_NOUNS) if " ".join(words) in aliases), None) if words else None
    if (words and noun is None) or (scale is None and noun is None):
        return None
    return scale, noun, suffix


def _unit_identity(unit: str) -> Any:
    """Equal for the same unit in any language: "Basispunkte" and "basis points"."""
    return _parse_unit(unit) or unit


def _unit_phrase(unit: str, number: str, language: str) -> tuple[str, set[str]] | None:
    """Spoken unit in the output language and its bare noun forms, or None for an unfamiliar unit."""
    parsed = _parse_unit(unit)
    if parsed is None:
        return None
    scale_index, noun_index, suffix = parsed
    single = number == "1"
    parts = []
    if scale_index is not None:
        forms = _UNIT_SCALES[scale_index][1]
        parts.append(forms.get(language, forms["en"])[0 if single else 1])
    noun: tuple[str, str] | None = None
    if noun_index is not None:
        forms = _UNIT_NOUNS[noun_index][1]
        noun = forms.get(language, forms["en"])
        # With a scale word the noun is plural ("1 million crowns"); alone it follows the number.
        parts.append(noun[0 if single and scale_index is None else 1])
    bare = {form.casefold() for form in noun} if noun and scale_index is None and not suffix else set()
    return " ".join(parts) + (_PER_HOUR_SPOKEN.get(language, _PER_HOUR) if suffix else ""), bare


def _spoken_amount(fact: dict[str, Any], figure: _Figure, source_language: str, language: str) -> str:
    number = _spoken_number(str(fact["value"]), source_language, language)
    canonical = _value_unit(fact["value"], "", source_language)[0]
    phrase = _unit_phrase(figure.unit, canonical, language)
    if phrase is None:
        return f"{number} {fact.get('unit') or figure.unit}".strip()
    unit, nouns = phrase
    # "Applications, 310." rather than "Applications, 310 applications."
    if figure.label and nouns & set(figure.label.casefold().split()):
        unit = ""
    return f"{number} {unit}".strip()


# A story is read as a few claims, not every number in it.
_MAX_SPOKEN_CLAIMS = 4
# Figures read out without a measure name, only for a story in which no figure can be named.
_UNNAMED_FIGURE_LIMIT = 3
_UNNAMED_FIGURE = {"en": "The report gives {}.", "de": "Der Bericht nennt {}.", "nl": "Het bericht noemt {}."}

_NARRATION_FRAMES = {
    "en": {"beginner": "In plain terms, here’s what the sources report.", "intermediate": "Here’s what the sources report.", "advanced": "Here are the reported measures."},
    "de": {"beginner": "Einfach gesagt steht Folgendes in den Quellen.", "intermediate": "Das berichten die Quellen.", "advanced": "Das sind die berichteten Kennzahlen."},
    "nl": {"beginner": "Eenvoudig gezegd melden de bronnen het volgende.", "intermediate": "Dit melden de bronnen.", "advanced": "Dit zijn de gerapporteerde cijfers."},
}


def narrate(ranked: list[dict[str, Any]], profile: dict[str, Any], providers: Any) -> list[dict[str, Any]]:
    """Emphasize the listener's interests while retaining the verified claims."""
    result = []
    duration = max(1, int(profile.get("duration", 5) or 5))
    output_language = str(profile.get("language", "en")).lower()
    expertise = str(profile.get("expertise", "intermediate")).lower()
    # Expertise sets the introduction; every claim is a "Label, amount." sentence whatever the style.
    # The style only guides a live model's arrangement.
    style = str(profile.get("style") or "neutral")
    frame = _NARRATION_FRAMES.get(output_language, _NARRATION_FRAMES["en"])
    introduction = frame.get(expertise, frame["intermediate"])
    for story in ranked:
        by_id = {str(a.get("article_id")): a for a in story.get("articles", [])}
        facts = _verified_facts(story)
        if not facts:
            continue
        source_languages = {
            fact["id"]: str(fact.get("language") or by_id[str(fact["article_id"])].get("language", "en"))
            for fact in facts
        }
        # Equivalent source facts share one spoken claim and keep all of their citations. Equal values
        # for different measures stay distinct.
        preferred = sorted(range(len(facts)), key=lambda n: source_languages[facts[n]["id"]] != output_language)
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for n in preferred:
            fact = facts[n]
            source_language = source_languages[fact["id"]]
            figure = _describe_figure(fact, source_language, output_language)
            if not figure.speakable:
                continue
            value = _value_unit(fact["value"], "", source_language)[0]
            group = groups.setdefault((figure.slot, value, _unit_identity(figure.unit)), {
                "fact": fact, "figure": figure, "source_language": source_language, "first": n, "members": [],
            })
            group["members"].append(fact)
            group["first"] = min(group["first"], n)
        # A figure whose evidence names no measure is not spoken under an invented label. Only when nothing
        # in the story can be named are the figures read out, with their units, as plain reports.
        candidates, limit = [g for g in groups.values() if g["figure"].label], _MAX_SPOKEN_CLAIMS
        if not candidates:
            candidates, limit = list(groups.values()), _UNNAMED_FIGURE_LIMIT
        if not candidates:
            continue

        def importance(group: dict[str, Any]) -> tuple[int, int]:
            conflicting = any(member.get("conflict_status") == "conflict" for member in group["members"])
            return (0 if conflicting else 1 if group["figure"].label and group["figure"].unit else 2, group["first"])

        # Conflicting values, then named figures with a unit. The choice depends only on the facts, so every
        # language, expertise and angle of a prepared summary cites the same ones, in source order.
        chosen = sorted(sorted(candidates, key=importance)[:limit], key=lambda group: group["first"])
        approved = [introduction]
        claim_facts: list[dict[str, Any]] = []
        for group in chosen:
            fact, figure = group["fact"], group["figure"]
            claim_facts.append(fact)
            amount = _spoken_amount(fact, figure, group["source_language"], output_language)
            if figure.label:
                approved.append(f"{figure.label[:1].upper()}{figure.label[1:]}, {amount}.")
            else:
                approved.append(_UNNAMED_FIGURE.get(output_language, _UNNAMED_FIGURE["en"]).format(amount))
        # Only facts that are spoken, or equal to one that is, are cited.
        members = {id(member) for group in chosen for member in group["members"]}
        spoken = [fact for fact in facts if id(fact) in members]
        ids = list(dict.fromkeys(str(f["id"]) for f in spoken))
        cited_ids = list(dict.fromkeys(str(f.get("article_id")) for f in spoken))
        sources = [by_id[aid] for aid in cited_ids if aid in by_id]
        conflicted = any(f.get("conflict_status") == "conflict" for f in spoken)
        # Shared summaries emphasize the listener's interests. A stable sort keeps
        # the source order when interests do not distinguish the claims.
        interests = [(_topic_key(topic), weight) for topic, weight in _positive_topic_weights(profile).items()]
        if interests and len(claim_facts) > 1:
            scores = [
                -sum(weight for topic, weight in interests if _mentions_topic(topic, str(fact.get("evidence", ""))))
                for fact in claim_facts
            ]
            order = sorted(range(len(claim_facts)), key=lambda index: scores[index])
            approved = [approved[0]] + [approved[index + 1] for index in order]
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
        result.append({"id": "segment-" + story["id"], "text": text, "fact_ids": ids, "sources": sources, "conflict": conflicted, "conflict_labels": list(dict.fromkeys(f.get("conflict_label") for f in spoken if f.get("conflict_label")))})
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
