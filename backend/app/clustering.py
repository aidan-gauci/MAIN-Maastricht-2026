"""Conservative local grouping based on dates, headlines and stable URLs."""
from collections import defaultdict
from datetime import date, datetime
from typing import NamedTuple
import math
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
    figures: frozenset = frozenset()
    edition: tuple = ("", "")   # (outlet, language) of the group's first article


# Figures survive translation, so shared body figures are strong evidence of one story. Separators
# are dropped so "3,25"/"3.25" and "12.400"/"12,400" match; one-digit numbers are too common to count.
_FIGURE = re.compile(r"\d+(?:[.,]\d+)*")
SAME_STORY_FIGURE_OVERLAP = 0.5     # headline-similar articles must also share this much of their figures
TRANSLATION_FIGURE_OVERLAP = 0.6    # a translation shares most of its figures with the original
TRANSLATION_WINDOW_SECONDS = 15 * 60
TRANSLATION_MIN_FIGURES = 3
# Translations with too few figures to match on them need a closer time, the same outlet and shared headline words.
# The organizers' pairs are published a median of 2.3 minutes apart (95% within 3.5; 6 of 238 beyond 5).
TRANSLATION_FEW_FIGURES_WINDOW_SECONDS = 5 * 60
TRANSLATION_MIN_SHARED_WORDS = 2
# One outlet's lookalike headlines are separate pieces (daily follow-ups, templated screens) unless the
# bodies are the same text. Measured on the event corpus: genuine duplicates share every figure, the
# templated and follow-up lookalikes share at most 75%.
SAME_EDITION_FIGURE_OVERLAP = 0.9


def body_figures(article, limit=6000):
    figures = (re.sub(r"[.,]", "", value) for value in _FIGURE.findall(str(article.get("body", ""))[:limit]))
    return frozenset(value for value in figures if len(value) >= 2)


def figure_overlap(first, second):
    """Share of the smaller figure set found in the other; 0 when either has none."""
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


def _timestamp(article):
    try:
        return datetime.fromisoformat(str(article.get("publication_date", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


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
        figures = body_figures(article)
        route = canonical_key(article)
        headline = tuple(tokens)
        group = by_route.get(route)

        edition = (outlet(article), _language(article))

        def same_story(idx):
            # Templated headlines ("Stock Market News for Nov 18") look alike across different
            # events; when both bodies carry several figures, require them to share most of them.
            # Short reports that disagree on one number still group, so the conflict is labelled.
            other = metadata[idx]
            rich = len(figures) >= TRANSLATION_MIN_FIGURES and len(other.figures) >= TRANSLATION_MIN_FIGURES
            overlap = figure_overlap(figures, other.figures)
            if other.edition == edition:
                # One outlet does not report an event twice in one language under lookalike headlines:
                # those are follow-ups or templated screens, unless the text is the same text.
                return rich and overlap >= SAME_EDITION_FIGURE_OVERLAP if day != other.day else (
                    not rich or overlap >= SAME_EDITION_FIGURE_OVERLAP)
            return not rich or overlap >= SAME_STORY_FIGURE_OVERLAP

        if group is None and anchors and len(words) >= 3:
            group = next((idx for idx in by_headline[headline] if abs(day - metadata[idx].day) <= 3 and anchors & metadata[idx].anchors
                          and not _disagree(tickers, metadata[idx].tickers) and same_story(idx)), None)
        if group is None and len(words) >= 4:
            candidates = set().union(*(by_anchor.get(token, set()) for token in anchors))
            matches = []
            for idx in candidates:
                other_day, other_words, other_numbers, other_anchors, other_tickers = metadata[idx][:5]
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
                if not same_story(idx):
                    continue
                similarity = len(shared_words) / len(union)
                if similarity >= 0.68 or (similarity >= 0.53 and numbers & other_numbers):
                    matches.append((similarity, -idx))
            if matches:
                group = -max(matches)[1]
        if group is None:
            group = len(groups)
            groups.append([])
            metadata.append(_Headline(day, words, numbers, anchors, tickers, figures, edition))
            by_headline[headline].append(group)
            for token in anchors:
                by_anchor[token].add(group)
        by_route[route] = group
        groups[group].append(article)

    return _join_translations(groups)


# Measured on the event corpus with cached embeddings (judging the merged pairs by headline, with figure
# overlap taken between the English leads): at overlap 0.35 every cosine from 0.79 to 0.82 and either window
# merges only reports of one event. Dropping the overlap to 0.25 merges digests and stories of a multi-day
# saga; cosine 0.78 adds a biography timeline. 0.79 with 36 hours adds one more genuine pair (an earnings
# report and its next-day call summary, 24.1 hours apart).
SAME_EVENT_SIMILARITY = 0.79        # embedding cosine for two outlets reporting one event
SAME_EVENT_WINDOW_SECONDS = 36 * 3600
SAME_EVENT_FIGURE_OVERLAP = 0.35    # same event shares figures; one ongoing topic usually does not

# A digest covers many events, so it is never "the same story" as one of them, however many figures they share.
_ROUNDUP = re.compile(
    r"\b(?:digest|round-?up|things to know|(?:stock market|company|market) news for|week ahead|weekly outlook|"
    r"(?:morning|evening) brief\w*)\b", re.I)


def is_roundup(article):
    return bool(_ROUNDUP.search(str(article.get("title", ""))))


def cosine(first, second):
    if not first or not second or len(first) != len(second):
        return 0.0
    dot = sum(a * b for a, b in zip(first, second))
    norm = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return dot / norm if norm else 0.0


def outlet(article):
    # "Invezz - German" and "Invezz" are one outlet's editions.
    return str(article.get("source", "")).split(" - ")[0].strip().casefold()


def merge_same_event(groups, vectors):
    """Join groups from different outlets that report the same event.

    Embeddings alone also match articles on one ongoing topic (a multi-day saga) and digests, which must stay
    separate stories, so a merge also needs a close publication time and shared body figures, and a digest
    never merges.
    Pairs are merged greedily from most to least similar, and each group joins at most one other,
    so chains of loosely related articles cannot snowball into one story.
    """
    if len(vectors) != len(groups):
        return groups
    leads = [next((a for a in group if _language(a) == "en"), group[0]) for group in groups]
    stamps = [_timestamp(lead) for lead in leads]
    # The leads' figures, not the union over a group: a group holding an English, German and Dutch edition
    # carries three articles' worth of figures, which inflates its overlap with any other report.
    figures = [body_figures(lead) for lead in leads]
    outlets = [{outlet(a) for a in group} for group in groups]
    roundups = [any(is_roundup(a) for a in group) for group in groups]
    order = sorted((i for i in range(len(groups)) if stamps[i]), key=lambda i: stamps[i])
    pairs = []
    for position, i in enumerate(order):
        for j in order[position + 1:]:
            if (stamps[j] - stamps[i]).total_seconds() > SAME_EVENT_WINDOW_SECONDS:
                break
            if outlets[i] & outlets[j] or roundups[i] or roundups[j]:
                continue
            if len(figures[i]) < TRANSLATION_MIN_FIGURES or len(figures[j]) < TRANSLATION_MIN_FIGURES:
                continue
            if figure_overlap(figures[i], figures[j]) < SAME_EVENT_FIGURE_OVERLAP:
                continue
            similarity = cosine(vectors[i], vectors[j])
            if similarity >= SAME_EVENT_SIMILARITY:
                pairs.append((similarity, i, j))
    target, used = list(range(len(groups))), set()
    for _, i, j in sorted(pairs, reverse=True):
        if i in used or j in used:
            continue
        target[j] = i
        used.update((i, j))
    joined = {}
    for idx, group in enumerate(groups):
        joined.setdefault(target[idx], []).extend(group)
    return [joined[idx] for idx in sorted(joined)]


def _language(article):
    return str(article.get("language") or "en").lower()[:2]


def _title_evidence(group, hub):
    """Headline words (numbers and names included) and tickers shared by the closest-worded pair of articles."""
    def words(article):
        tokens = set(_headline(str(article.get("title", ""))))
        return tokens | {f"ticker:{t.casefold()}" for t in article.get("tickers", []) if str(t).strip()}
    hub_words = [words(a) for a in hub]
    return max(len(words(a) & other) for a in group for other in hub_words)


def _join_translations(groups):
    """Attach groups that translate another-language group published within minutes.

    Publishers release translated editions with different headlines and URLs, so headline matching
    misses them. A translation appears within minutes of its original and repeats its figures.
    When a short article has too few figures to match on, it needs the same outlet, a closer time
    and shared headline words, and may not contradict the figures it does have.
    """
    profiles = []
    for group in groups:
        figures = frozenset().union(*(body_figures(article) for article in group))
        stamps = [stamp for stamp in map(_timestamp, group) if stamp]
        profiles.append((frozenset(map(_language, group)), figures, stamps))
    target = list(range(len(groups)))
    # Join into groups that contain English (the corpus's main language), else into the earliest.
    hubs = [idx for idx, (languages, _, _) in enumerate(profiles) if "en" in languages]
    attached = defaultdict(set)     # languages already joined into each hub
    unmatched = []
    for idx, (languages, figures, stamps) in enumerate(profiles):
        if "en" in languages or not stamps:
            continue
        best, best_overlap = None, TRANSLATION_FIGURE_OVERLAP
        for hub in hubs:
            hub_languages, hub_figures, hub_stamps = profiles[hub]
            if languages & hub_languages or min(len(figures), len(hub_figures)) < TRANSLATION_MIN_FIGURES:
                continue
            if not any(abs((a - b).total_seconds()) <= TRANSLATION_WINDOW_SECONDS for a in stamps for b in hub_stamps):
                continue
            overlap = figure_overlap(figures, hub_figures)
            if overlap >= best_overlap and (best is None or overlap > best_overlap):
                best, best_overlap = hub, overlap
        if best is not None:
            target[idx] = best
            attached[best] |= languages
        else:
            unmatched.append(idx)

    candidates = {}
    for idx in unmatched:
        languages, figures, stamps = profiles[idx]
        outlets = {outlet(a) for a in groups[idx]}
        found = []
        for hub in hubs:
            hub_languages, hub_figures, hub_stamps = profiles[hub]
            if languages & (hub_languages | attached[hub]) or min(len(figures), len(hub_figures)) >= TRANSLATION_MIN_FIGURES:
                continue
            if not outlets & {outlet(a) for a in groups[hub]}:
                continue
            gap = min(abs((a - b).total_seconds()) for a in stamps for b in hub_stamps)
            if gap > TRANSLATION_FEW_FIGURES_WINDOW_SECONDS:
                continue
            if figures and hub_figures and figure_overlap(figures, hub_figures) < TRANSLATION_FIGURE_OVERLAP:
                continue
            evidence = _title_evidence(groups[idx], groups[hub])
            if evidence >= TRANSLATION_MIN_SHARED_WORDS:
                found.append((-evidence, gap, hub))
        found.sort()
        # An article that fits two originals equally well is left alone rather than guessed.
        if found and (len(found) == 1 or found[0][0] < found[1][0]):
            candidates[idx] = found[0]
    for idx, (_, _, hub) in sorted(candidates.items(), key=lambda item: item[1][:2]):
        languages = profiles[idx][0]
        if not languages & attached[hub]:
            target[idx] = hub
            attached[hub] |= languages
    joined = {}
    for idx, group in enumerate(groups):
        joined.setdefault(target[idx], []).extend(group)
    return [joined[idx] for idx in sorted(joined)]
