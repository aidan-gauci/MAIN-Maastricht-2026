"""Re-cluster an imported corpus under the current grouping rules, without any provider call.

Facts are tied to the article they were extracted from, so stories can be rebuilt from the facts the
import already paid for: the articles are grouped again (and, when the import stored story embeddings,
cross-outlet reports of one event are merged again), each new story takes the union of its members'
stored facts, and conflicts, thread markers and summaries are recomputed. The stored article rows keep
no body text, so the original feeds are read again for the headline, figure and topic checks.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from . import pipeline
from .clustering import group_articles, merge_same_event
from .summaries import prepare_summaries
from .threads import assign_threads

_CONFLICT_FIELDS = ("conflict_status", "conflict_group", "conflict_label")
_MAX_LISTED_CHANGES = 12


def _lead(group: list[dict[str, Any]]) -> dict[str, Any]:
    """The most recent English article (the story's headline), else the most recent article."""
    english = [a for a in group if pipeline._language(a) == "en"] or group
    return max(english, key=lambda a: (str(a.get("publication_date", "")), str(a["article_id"])))


def _fresh_facts(articles: list[str], facts_by_article: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    facts, known = [], set()
    for article_id in articles:
        for stored in facts_by_article.get(article_id, []):
            if stored["id"] in known:
                continue
            known.add(stored["id"])
            fact = {key: value for key, value in stored.items() if key not in _CONFLICT_FIELDS}
            fact["conflict_status"] = "supported"
            # Two facts from one sentence are not competing claims; the stored hash identifies the sentence.
            fact["_claim_group"] = (article_id, stored.get("source_sentence_hash") or stored.get("evidence", ""))
            facts.append(fact)
    return facts


def regroup_corpus(store, articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the stored stories under the current rules and publish them in one transaction.

    ``articles`` are the feeds the corpus was imported from (any superset works). Raises ``ValueError``
    before anything is written when the stored corpus cannot be rebuilt, so the previous corpus is kept.
    """
    stored = store.stories()
    if not stored:
        raise ValueError("Nothing to regroup: import a corpus for this mode first")
    rows = {row["article_id"]: row for row in store.articles()}
    available = {str(a["article_id"]): a for a in articles}
    missing = sorted(set(rows) - set(available))
    if missing:
        raise ValueError(f"The feeds are missing {len(missing)} imported articles (for example {missing[0]}); "
                         "pass the same files the corpus was imported from")
    source = []
    for article in articles:
        row = rows.get(str(article["article_id"]))
        if row is None:
            continue
        merged = dict(article, official=bool(row.get("official")))
        for key in ("region", "country"):
            if row.get(key):
                merged[key] = row[key]
        source.append(merged)
    by_id = {str(a["article_id"]): a for a in source}

    old_members = {story["id"]: {str(a["article_id"]) for a in story["articles"]} for story in stored}
    old_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for story in stored:
        for article_id in old_members[story["id"]]:
            old_of[article_id].append(story)
    facts_by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for story in stored:
        for fact in story.get("facts", []):
            if (fact["article_id"], fact["id"]) not in seen:
                seen.add((fact["article_id"], fact["id"]))
                facts_by_article[fact["article_id"]].append(fact)
    # One vector is stored per story, so every member article carries it (see the README).
    vector_of = {article_id: story["embedding"] for story in stored if story.get("embedding")
                 for article_id in old_members[story["id"]]}

    groups = group_articles(source, pipeline._group_key, pipeline._day)
    cross_outlet = 0
    if vector_of:
        vectors = []
        for group in groups:
            ordered = [_lead(group)] + group
            vectors.append(next((vector_of[str(a["article_id"])] for a in ordered if str(a["article_id"]) in vector_of), []))
        joined = merge_same_event(groups, vectors)
        cross_outlet, groups = len(groups) - len(joined), joined

    stories, unchanged, dropped = [], 0, 0
    for group in groups:
        ids = sorted({str(a["article_id"]) for a in group})
        olds = {story["id"]: story for article_id in ids for story in old_of.get(article_id, [])}
        if not olds:
            continue    # none of these articles was in a story before, so the import dropped them
        if len(olds) == 1 and old_members[next(iter(olds))] == set(ids):
            stories.append(deepcopy(next(iter(olds.values()))))
            unchanged += 1
            continue
        facts = _fresh_facts(ids, facts_by_article)
        if not facts and any(story.get("facts") for story in olds.values()):
            dropped += len(ids)     # a live import keeps no story without a verified fact
            continue
        lead = _lead(group)
        whole = [story for story in olds.values() if old_members[story["id"]] <= set(ids)]
        live_origin = any("embedding" in old for old in whole)
        # A live import titles stories itself (in English); a local one used whichever article led, so
        # the English lead's own headline is used instead.
        title = next((story["title"] for story in whole if live_origin and str(lead["article_id"]) in old_members[story["id"]]),
                     lead["title"])
        story = {
            "id": pipeline._story_id(ids, title, pipeline._day(lead)),
            "title": str(title),
            "articles": [pipeline._article_reference(by_id[article_id]) for article_id in ids],
            "facts": facts,
            "publication_date": max(pipeline._day(by_id[article_id]) for article_id in ids),
        }
        story["conflicts"] = pipeline._label_conflicts(facts)
        if live_origin:
            # Topics a live import stored come from its extraction; keep them across merges.
            story["topics"] = [t for t in dict.fromkeys(t for old in whole for t in old.get("topics", []))
                               if t != "news"] or ["news"]
        else:
            story["topics"], passing = pipeline._topics([by_id[article_id] for article_id in ids])
            if passing:
                story["passing_topics"] = passing
        vector = next((vector_of[a] for a in [str(lead["article_id"]), *ids] if a in vector_of), None)
        if vector:
            story["embedding"] = vector
        stories.append(story)
    if not stories:
        raise ValueError("Regrouping produced no stories; the previous corpus was kept")

    threads = assign_threads(stories)
    summaries = prepare_summaries(stories)
    store.replace_corpus(store.articles(), stories, summaries)

    new_of = {str(a["article_id"]): story["id"] for story in stories for a in story["articles"]}
    merged = [story for story in stories if len({s["id"] for a in story["articles"] for s in old_of.get(str(a["article_id"]), [])}) > 1]
    split = [story for story in stored if len({new_of.get(article_id) for article_id in old_members[story["id"]]} - {None}) > 1]
    new_titles = {story["id"]: story["title"] for story in stories}
    changes = [{"kind": "merged", "into": _short(story["title"]), "articles": len(story["articles"])} for story in merged]
    changes += [{"kind": "split", "from": _short(story["title"]),
                 "into": [_short(new_titles[i]) for i in sorted({new_of[a] for a in old_members[story["id"]] if a in new_of})]}
                for story in split]
    return {
        "stories_before": len(stored),
        "stories_after": len(stories),
        "unchanged": unchanged,
        "merged_stories": len(merged),
        "split_stories": len(split),
        "cross_outlet_merges": cross_outlet,
        "articles_without_verified_facts": dropped,
        "threads": len(threads),
        "stories_in_threads": sum(len(thread) for thread in threads),
        "used_story_embeddings": bool(vector_of),
        "changes": changes[:_MAX_LISTED_CHANGES],
    }


def _short(title: str, width: int = 70) -> str:
    title = " ".join(str(title).split())
    return title if len(title) <= width else title[: width - 3] + "..."
