"""Event threads: stories that are developments of one ongoing event.

One developing event (a CEO's exit, a lawsuit and its fallout) is reported in many pieces over several
days. Each piece stays its own story, because the developments and their facts differ, but a briefing
should play one of them rather than twelve. A thread only marks that relation with a shared
``thread_id`` and a ``thread_title``; nothing is merged.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from .clustering import _STOP, _headline, _language, cosine, is_roundup

THREAD_WINDOW_DAYS = 5
THREAD_SIMILARITY = 0.65        # embedding cosine between two stories of one thread
THREAD_RARE_SHARE = 0.03        # a name is distinctive when at most this share of stories mention it
THREAD_PAIR_SHARE = 0.10        # two shared names, each at most this common, are also enough
THREAD_BURST_SHARE = 0.6        # share of a name's mentions that must fall in one thread-sized window
THREAD_MIN_WORDS = 4            # without embeddings: headline words (names included) the two stories share

_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9&]*")
_NOT_NAMES = set(
    "january february march april may june july august september october november december monday tuesday "
    "wednesday thursday friday saturday sunday here could can should would just after before over under amid "
    "top best why how what when where who which this that these those more most new says said".split()
)


def _english_titles(story: dict[str, Any]) -> list[str]:
    articles = story.get("articles") or []
    if not articles:
        return [str(story.get("title", ""))]
    return [str(a.get("title", "")) for a in articles if _language(a) == "en"]


def _names(titles: list[str], common: set[str]) -> set[str]:
    """Capitalised words of English headlines that never appear in lower case in any headline."""
    found = set()
    for title in titles:
        for token in _NAME_TOKEN.findall(title.replace("’", "'")):
            key = token.casefold()
            if token[0].isupper() and len(token) >= 3 and key not in common and key not in _NOT_NAMES and key not in _STOP:
                found.add(key)
    return found


def _bursty(days: list[int]) -> bool:
    """Most mentions fall inside one thread-sized window."""
    days = sorted(days)
    best, start = 0, 0
    for end, day in enumerate(days):
        while day - days[start] > THREAD_WINDOW_DAYS:
            start += 1
        best = max(best, end - start + 1)
    return best >= THREAD_BURST_SHARE * len(days)


def assign_threads(stories: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Mark stories of one developing event with a shared ``thread_id`` and ``thread_title``.

    Two stories belong together when they are published within a few days of each other, name the same
    distinctive person, company or ticker in their English headlines, and are about the same thing
    (embedding cosine when both stories have one, else enough shared headline words). A name common to
    many stories (a big company, a central bank) is not distinctive on its own: it takes a rare name, or
    two shared names. Digests never join a thread. Threads are the connected components of that relation;
    each marks only stories of size two or more. Markers of an earlier assignment are cleared first.
    Returns the threads as lists of stories, oldest first.
    """
    for story in stories:
        story.pop("thread_id", None)
        story.pop("thread_title", None)
    titles = [_english_titles(story) for story in stories]
    common = {token for row in titles for title in row for token in _NAME_TOKEN.findall(title) if token.islower()}
    names = [_names(row, common) for row in titles]
    days: list[int | None] = []
    for story in stories:
        try:
            days.append(date.fromisoformat(str(story.get("publication_date", ""))[:10]).toordinal())
        except ValueError:
            days.append(None)
    usable = [i for i in range(len(stories))
              if days[i] is not None and names[i] and not any(is_roundup({"title": t}) for t in titles[i])]
    frequency = Counter(name for i in usable for name in names[i])
    rare = max(2, THREAD_RARE_SHARE * len(stories))
    shared_cap = max(3, THREAD_PAIR_SHARE * len(stories))
    holders: dict[str, list[int]] = defaultdict(list)
    for i in usable:
        for name in names[i]:
            if 2 <= frequency[name] <= shared_cap:
                holders[name].append(i)
    # An event is a burst of coverage. A name spread over months (a fund family, a ticker that keeps
    # coming up) is an ongoing subject, however rarely it appears.
    for name in [name for name, members in holders.items() if not _bursty([days[i] for i in members])]:
        del holders[name]
    candidates: dict[tuple[int, int], set[str]] = defaultdict(set)
    for name, members in holders.items():
        for position, i in enumerate(members):
            for j in members[position + 1:]:
                candidates[(i, j)].add(name)

    edges = []
    for (i, j), shared in candidates.items():
        if abs(days[i] - days[j]) > THREAD_WINDOW_DAYS:
            continue
        if not (any(frequency[name] <= rare for name in shared) or len(shared) >= 2):
            continue
        first, second = stories[i].get("embedding"), stories[j].get("embedding")
        if first and second:
            related = cosine(first, second) >= THREAD_SIMILARITY
        else:
            words = [{word for title in titles[k] for word in _headline(title)} for k in (i, j)]
            related = len(words[0] & words[1]) >= THREAD_MIN_WORDS
        if related:
            edges.append((i, j, shared))

    # A thread is anchored on one name: the stories linked through that name. Chaining across names would
    # join separate events through a story that happens to mention both (an IPO story naming a rival).
    links: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    for i, j, shared in edges:
        for name in shared:
            links[name][i].add(j)
            links[name][j].add(i)
    anchored: list[tuple[str, set[int]]] = []
    for name, graph in links.items():
        seen: set[int] = set()
        for start in graph:
            if start in seen:
                continue
            component, pending = set(), [start]
            while pending:
                node = pending.pop()
                if node not in component:
                    component.add(node)
                    pending.extend(graph[node])
            seen |= component
            anchored.append((name, component))
    # A story joins the largest group it is in (then the rarer name), so a story naming two events
    # goes with the bigger one.
    choice: dict[int, tuple[tuple[int, int, str], int]] = {}
    for index, (name, component) in enumerate(anchored):
        rank = (-len(component), frequency[name], name)
        for i in component:
            if i not in choice or rank < choice[i][0]:
                choice[i] = (rank, index)
    chosen: dict[int, list[int]] = defaultdict(list)
    for i, (_, index) in choice.items():
        chosen[index].append(i)

    threads = []
    for members in chosen.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: (days[i], str(stories[i].get("id", ""))))
        inside = set(members)
        degree = Counter(i for a, b, _ in edges if a in inside and b in inside for i in (a, b))
        # The most connected story names the thread; the earliest wins a tie.
        central = max(members, key=lambda i: (degree[i], -members.index(i)))
        identity = str(stories[members[0]].get("id", "")) or str(days[members[0]])
        thread_id = "thread-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
        for i in members:
            stories[i]["thread_id"] = thread_id
            stories[i]["thread_title"] = str(stories[central].get("title", ""))
        threads.append([stories[i] for i in members])
    threads.sort(key=lambda thread: (str(thread[0].get("publication_date", "")), thread[0].get("thread_id", "")))
    return threads

