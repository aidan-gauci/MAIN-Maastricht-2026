"""Conservative local grouping based on dates, headlines and stable URLs."""
from collections import defaultdict
from datetime import date
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
    "quartal": "quarter", "kwartaal": "quarter", "dollar": "usd",
}
_GENERIC = set(
    "market markets stocks stock shares company price prices news economy rates growth forecast "
    "earnings investor investors quarter profit revenue billion million usd november today week "
    "rises falls report reports cuts update says ahead results financial".split()
)


def _headline(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    ascii_text = re.sub(r"(?<=\d),(?=\d{1,2}\b)", ".", ascii_text)
    ascii_text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", ascii_text)
    return [_ALIASES.get(word, word) for word in re.findall(r"[a-z]+|\d+(?:\.\d+)?", ascii_text)
            if word not in _STOP]


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
        anchors.update(f"ticker:{ticker.lower()}" for ticker in article.get("tickers", []))
        route = canonical_key(article)
        headline = tuple(tokens)
        group = by_route.get(route)
        if group is None and len(words) >= 3:
            group = next((idx for idx in by_headline[headline] if abs(day - metadata[idx][0]) <= 3), None)
        if group is None and len(words) >= 4:
            candidates = set().union(*(by_anchor.get(token, set()) for token in anchors))
            matches = []
            for idx in candidates:
                other_day, other_words, other_numbers, other_anchors = metadata[idx]
                shared_words = words & other_words
                union = words | other_words
                if abs(day - other_day) > 2 or len(shared_words) < 4 or not (anchors & other_anchors):
                    continue
                if numbers and other_numbers and numbers.isdisjoint(other_numbers):
                    continue
                similarity = len(shared_words) / len(union)
                if similarity >= 0.68 or (similarity >= 0.53 and numbers & other_numbers):
                    matches.append((similarity, -idx))
            if matches:
                group = -max(matches)[1]
        if group is None:
            group = len(groups)
            groups.append([])
            metadata.append((day, words, numbers, anchors))
            by_headline[headline].append(group)
            for token in anchors:
                by_anchor[token].add(group)
        by_route[route] = group
        groups[group].append(article)

    return groups
