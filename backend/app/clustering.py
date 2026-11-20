"""Conservative local grouping based on dates, headlines and stable URLs."""
from collections import defaultdict
from datetime import date
from typing import NamedTuple
import re
import unicodedata


_STOP = set(
    "a an and as at be but by for from has have how in into is it its new of on or out said "
    "the to up was were what when where who why will with you your "
    "am auf aus bei das der des die ein eine fuer hat im in ist mit nach sich sind und von vor zu zum zur "
    "aan als bij dan dat de een en het hoe in is met na naar om op te uit van voor wordt zijn".split()
)
_ALIASES = {
    "aktien": "stocks", "aandelen": "stocks", "aktie": "stock", "aandeel": "stock",
    "aktienkurs": "shares", "aandelenkoers": "shares",
    "zentralbank": "centralbank", "leitzins": "rates", "rente": "rates",
    "senkt": "cuts", "verlaagt": "cuts", "senken": "cuts",
    "steigt": "rises", "stijgt": "rises", "fallt": "falls", "daalt": "falls",
    "gewinn": "profit", "winst": "profit", "umsatz": "revenue", "omzet": "revenue",
    "wachstum": "growth", "groei": "growth", "prognose": "forecast", "verwachting": "forecast",
    "quarterly": "quarter", "quartalsumsatz": "quarter revenue", "kwartaalomzet": "quarter revenue",
    "wachstumsprognose": "growth forecast", "groeiverwachting": "growth forecast",
    "rate": "rates", "beleidsrente": "rates", "houdt": "holds", "halt": "holds",
    "meldet": "reports", "meldde": "reports", "reported": "reports",
    "posts": "reports", "posted": "reports", "publishes": "reports", "announces": "reports",
    "milliarden": "billion", "miljard": "billion", "millionen": "million", "miljoen": "million",
    "euros": "euro", "prozent": "percent", "procent": "percent", "%": "percent",
    "quartal": "quarter", "kwartaal": "quarter", "dollar": "usd",
}
_GENERIC = set(
    "market markets stocks stock shares company price prices news economy rates growth forecast "
    "earnings investor investors quarter profit revenue billion million usd november today week "
    "rises falls report reports cuts update says ahead results financial euro percent holds policy centralbank "
    "annual monthly preliminary latest expects raises".split()
)

_EVENT_AXES = ({"revenue", "profit"}, {"holds", "cuts", "raises"}, {"rises", "falls"},
               {"q1", "q2", "q3", "q4"})


class _Headline(NamedTuple):
    day: int
    words: set
    numbers: set
    anchors: set
    tickers: set


def _disagree(first, second):
    return bool(first and second and first.isdisjoint(second))


def _headline(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    ascii_text = re.sub(r"(?<=\d),(?=\d{1,2}\b)", ".", ascii_text)
    ascii_text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", ascii_text)
    ascii_text = re.sub(r"\b(?:central|centrale) bank\b", "centralbank", ascii_text)
    tokens = re.findall(r"q[1-4]\b|[a-z]+|\d+(?:\.\d+)?|%", ascii_text)
    return [word for token in tokens if token not in _STOP
            for word in _ALIASES.get(token, token).split()]


def group_articles(articles, canonical_key, publication_day):
    """Return groups, using each group's first article as the similarity anchor."""
    groups = []
    by_route = {}
    by_headline = defaultdict(list)
    by_anchor = defaultdict(set)
    metadata = []

    for article in articles:
        day = date.fromisoformat(publication_day(article)).toordinal()
        tokens = _headline(article["title"])
        words = set(tokens)
        numbers = {token for token in words if token[0].isdigit()}
        anchors = {token for token in words - numbers - _GENERIC if len(token) >= 3}
        tickers = {ticker.casefold() for ticker in article.get("tickers", []) if ticker.strip()}
        anchors.update(f"ticker:{ticker}" for ticker in tickers)
        route = canonical_key(article)
        headline = tuple(tokens)
        group = by_route.get(route)
        if group is None and anchors and len(words) >= 3:
            group = next((idx for idx in by_headline[headline] if abs(day - metadata[idx].day) <= 3 and anchors & metadata[idx].anchors
                          and not _disagree(tickers, metadata[idx].tickers)), None)
        if group is None and len(words) >= 4:
            candidates = set().union(*(by_anchor.get(token, set()) for token in anchors))
            matches = []
            for idx in candidates:
                other_day, other_words, other_numbers, other_anchors, other_tickers = metadata[idx]
                shared_words = words & other_words
                union = words | other_words
                if abs(day - other_day) > 2 or len(shared_words) < 4 or not (anchors & other_anchors):
                    continue
                if _disagree(tickers, other_tickers) or _disagree(numbers, other_numbers):
                    continue
                # Shared boilerplate and a number are weak evidence when
                # the headlines identify different measures or periods.
                if any(_disagree(words & axis, other_words & axis) for axis in _EVENT_AXES):
                    continue
                similarity = len(shared_words) / len(union)
                if similarity >= 0.68 or (similarity >= 0.53 and numbers & other_numbers):
                    matches.append((similarity, -idx))
            if matches:
                group = -max(matches)[1]
        if group is None:
            group = len(groups)
            groups.append([])
            metadata.append(_Headline(day, words, numbers, anchors, tickers))
            by_headline[headline].append(group)
            for token in anchors:
                by_anchor[token].add(group)
        by_route[route] = group
        groups[group].append(article)

    return groups
