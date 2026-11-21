"""Offline check of story grouping against the organizers' translation ground truth.

Nothing here calls a provider. The ground truth is the organizers' own: a German or Dutch article
and an English article by the same author, from the same outlet, published within 15 minutes of
each other are one story in two languages. The importer drops the author, so it is read again
from the raw feeds. Reports list titles only; no article text is produced.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .clustering import (
    TRANSLATION_FIGURE_OVERLAP, TRANSLATION_MIN_FIGURES, body_figures, figure_overlap, merge_same_event, outlet,
)
from .importer import read_articles, read_authors

TRUTH_WINDOW_SECONDS = 15 * 60
TITLE_WIDTH = 78


def _stamp(article: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(article.get("publication_date", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _language(article: dict[str, Any]) -> str:
    return str(article.get("language") or "en").lower()[:2]


def translation_truth(articles: list[dict[str, Any]], authors: dict[str, str],
                      sources: dict[str, str] | None = None) -> dict[str, set[str]]:
    """Non-English article ID -> its English original (a one-element set).

    Same author, same outlet family, within 15 minutes; when an author published two English pieces in
    that window, the nearest in time. A few translations are credited to someone else than the original
    (the English piece carries a team byline); for those, the nearest English article of the same outlet
    within 15 minutes that shares most of its figures counts as the original. ``sources`` (optional) is
    filled with how each original was found: "author" or "figures".
    """
    english = [(a, _stamp(a)) for a in articles if _language(a) == "en"]
    english = [(a, stamp) for a, stamp in english if stamp is not None]
    truth = {}
    for article in articles:
        stamp = _stamp(article)
        if _language(article) == "en" or stamp is None:
            continue
        near = [(abs((other_stamp - stamp).total_seconds()), other) for other, other_stamp in english
                if outlet(other) == outlet(article)
                and abs((other_stamp - stamp).total_seconds()) <= TRUTH_WINDOW_SECONDS]
        author = authors.get(article["article_id"])
        by_author = [(gap, other) for gap, other in near if author and authors.get(other["article_id"]) == author]
        how, pool = "author", by_author
        if not pool:
            figures = body_figures(article)
            how, pool = "figures", [
                (gap, other) for gap, other in near
                if min(len(figures), len(body_figures(other))) >= TRANSLATION_MIN_FIGURES
                and figure_overlap(figures, body_figures(other)) >= TRANSLATION_FIGURE_OVERLAP]
        if pool:
            truth[article["article_id"]] = {min(pool, key=lambda item: (item[0], item[1]["article_id"]))[1]["article_id"]}
            if sources is not None:
                sources[article["article_id"]] = how
    return truth


def _lead(group):
    return next((a for a in group if _language(a) == "en"), group[0])


def group_vectors(groups, embeddings):
    """One vector per group from a list aligned with the groups, or an article-ID mapping."""
    if isinstance(embeddings, list):
        return embeddings if len(embeddings) == len(groups) else None
    vectors = []
    for group in groups:
        ordered = [_lead(group)] + [a for a in group if a is not _lead(group)]
        vectors.append(next((embeddings[a["article_id"]] for a in ordered if a["article_id"] in embeddings), []))
    return vectors


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= TITLE_WIDTH else text[:TITLE_WIDTH - 3] + "..."


def _describe(group):
    return [f"{_language(a)} {outlet(a)} {str(a.get('publication_date', ''))[:16]} {_short(a['title'])}" for a in group]


def _days(group):
    return sorted({str(a.get("publication_date", ""))[:10] for a in group})


def thread_report(final, embeddings=None, probes=()):
    """Event threads over the stories these groups would become (a story per group).

    ``probes`` are words to look for in English headlines: for each, how many stories mention it and how
    many of those sit in a thread, so a known saga can be checked without listing its articles.
    """
    from .pipeline import _day, _story_id
    from .threads import assign_threads

    vectors = group_vectors(final, embeddings) if embeddings is not None else [[]] * len(final)
    stories = []
    for group, vector in zip(final, vectors):
        lead = _lead(group)
        story = {"id": _story_id([a["article_id"] for a in group]), "title": lead["title"],
                 "articles": [{"article_id": a["article_id"], "title": a["title"], "language": _language(a)} for a in group],
                 "publication_date": max(_day(a) for a in group)}
        if vector:
            story["embedding"] = vector
        stories.append(story)
    threads = assign_threads(stories)
    result: dict[str, Any] = {
        "threads": len(threads),
        "stories_in_threads": sum(len(thread) for thread in threads),
        "size_distribution": dict(sorted(Counter(len(thread) for thread in threads).items())),
        "largest": [{"size": len(thread), "title": _short(thread[0]["thread_title"])}
                    for thread in sorted(threads, key=len, reverse=True)[:8]],
        "used_embeddings": embeddings is not None,
        "probes": {},
    }
    for word in probes:
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.I)
        hits = [s for s in stories if any(pattern.search(a["title"]) for a in s["articles"] if a["language"] == "en")]
        in_threads = [s for s in hits if s.get("thread_id")]
        result["probes"][word] = {
            "stories": len(hits), "in_a_thread": len(in_threads),
            "threads": dict(Counter(_short(s["thread_title"]) for s in in_threads)),
            "outside": [_short(s["title"]) for s in hits if not s.get("thread_id")],
        }
    return result


def evaluate_grouping(articles, groups, authors=None, joined_groups=None, embeddings=None, probes=()) -> dict[str, Any]:
    """Summarize ``groups`` (lists of articles) and, when authors are known, score translation matching.

    ``joined_groups`` is the same corpus after the cross-outlet merge, when embeddings were supplied.
    """
    final = joined_groups or groups
    by_id = {a["article_id"]: a for a in articles}
    report: dict[str, Any] = {
        "articles": len(articles),
        "languages": dict(Counter(_language(a) for a in articles)),
        "groups": len(final),
        "size_distribution": dict(sorted(Counter(len(group) for group in final).items())),
    }
    if joined_groups is not None:
        report["groups_before_cross_outlet_merge"] = len(groups)
        report["cross_outlet_merges"] = len(groups) - len(joined_groups)

    if authors:
        # Translation matching is judged before the cross-outlet merge, which is judged on its own.
        # Only German and Dutch articles the organizers pair with an English original are scored.
        how = {}
        truth = translation_truth(articles, authors, how)
        in_group = {a["article_id"]: index for index, group in enumerate(groups) for a in group}
        recalled = {i for i, candidates in truth.items() if any(in_group[c] == in_group[i] for c in candidates)}
        predicted = [(a["article_id"], b["article_id"]) for group in groups for a in group if _language(a) != "en"
                     for b in group if _language(b) == "en"]
        scored = [pair for pair in predicted if pair[0] in truth]
        correct = {pair for pair in scored if pair[1] in truth[pair[0]]}
        joined = {i for i, _ in scored}
        right = {i for i, _ in correct}
        report["translation"] = {
            "truth_articles": len(truth),
            "truth_found_by": dict(Counter(how.values())),
            "recalled": len(recalled),
            "recall": round(len(recalled) / len(truth), 4) if truth else None,
            "joined_articles": len(joined),
            "joined_to_a_true_original": len(right),
            "article_precision": round(len(right) / len(joined), 4) if joined else None,
            "predicted_pairs": len(scored),
            "correct_pairs": len(correct),
            "pair_precision": round(len(correct) / len(scored), 4) if scored else None,
            "false_joins": [[_short(by_id[i]["title"]), _short(by_id[c]["title"])] for i, c in scored if (i, c) not in correct],
            "missed": [{"language": _language(by_id[i]), "published": str(by_id[i]["publication_date"])[:16],
                        "title": _short(by_id[i]["title"])} for i in truth if i not in recalled],
            "without_ground_truth_counterpart": [
                {"language": _language(a), "published": str(a["publication_date"])[:16], "title": _short(a["title"]),
                 "grouped_with_english": any(_language(b) == "en" for b in groups[in_group[a["article_id"]]])}
                for a in articles if _language(a) != "en" and a["article_id"] not in truth],
        }

    multi_day = [group for group in final if len(_days(group)) > 1]
    same_outlet = [group for group in final if max(Counter((outlet(a), _language(a)) for a in group).values()) > 1]
    big = [group for group in groups if len(group) >= 4]
    report["multi_day_groups"] = len(multi_day)
    report["same_outlet_same_language_groups"] = len(same_outlet)
    report["suspicious"] = [
        {"why": why, "size": len(group), "articles": _describe(group)}
        for why, groups_ in (("spans several days", multi_day), ("same outlet and language twice", same_outlet),
                             ("four or more articles before any cross-outlet merge", big))
        for group in groups_
    ]
    if joined_groups is not None:
        before = {a["article_id"]: i for i, group in enumerate(groups) for a in group}
        merged = [group for group in joined_groups if len({before[a["article_id"]] for a in group}) > 1]
        report["cross_outlet_merged_groups"] = [_describe(group) for group in merged]
    report["event_threads"] = thread_report(final, embeddings, probes)
    return report


def render(report: dict[str, Any]) -> str:
    lines = [f"articles {report['articles']} {report['languages']}",
             f"groups {report['groups']} (sizes {report['size_distribution']})"]
    if "cross_outlet_merges" in report:
        lines.append(f"cross-outlet merges {report['cross_outlet_merges']} "
                     f"({report['groups_before_cross_outlet_merge']} groups before)")
    if "translation" in report:
        t = report["translation"]
        lines += [
            f"translations: {t['recalled']}/{t['truth_articles']} recalled (recall {t['recall']}), "
            f"{t['joined_to_a_true_original']}/{t['joined_articles']} joined articles sit with a true original "
            f"(precision {t['article_precision']}), {t['correct_pairs']}/{t['predicted_pairs']} pairs right "
            f"(pair precision {t['pair_precision']}), {len(t['false_joins'])} wrong pairs",
        ]
        lines += [f"  missed {m['language']} {m['published']} {m['title']}" for m in t["missed"]]
        lines += [f"  wrong pair: {a}  <->  {b}" for a, b in t["false_joins"]]
        lines.append(f"  ground truth found by {t['truth_found_by']}; {len(t['without_ground_truth_counterpart'])} "
                     "German/Dutch articles have no counterpart (not scored)")
        lines += [f"    {m['language']} {m['published']} {m['title']} (grouped with an English article: "
                  f"{m['grouped_with_english']})" for m in t["without_ground_truth_counterpart"]]
    lines.append(f"multi-day groups {report['multi_day_groups']}; same outlet and language twice "
                 f"{report['same_outlet_same_language_groups']}")
    for item in report["suspicious"]:
        lines.append(f"- {item['why']} ({item['size']} articles)")
        lines += [f"    {row}" for row in item["articles"]]
    for group in report.get("cross_outlet_merged_groups", []):
        lines.append("- cross-outlet merge")
        lines += [f"    {row}" for row in group]
    threads = report["event_threads"]
    lines.append(f"event threads {threads['threads']} holding {threads['stories_in_threads']} stories "
                 f"(sizes {threads['size_distribution']}; embeddings used: {threads['used_embeddings']})")
    lines += [f"    {row['size']:>3} stories: {row['title']}" for row in threads["largest"]]
    for word, probe in threads["probes"].items():
        lines.append(f"  probe '{word}': {probe['in_a_thread']} of {probe['stories']} stories in a thread {probe['threads']}")
        lines += [f"      outside: {title}" for title in probe["outside"]]
    return "\n".join(lines)


def run_evaluation(path: Path, embeddings_path: Path | None = None, *, as_json: bool = False, probes=()) -> str:
    # Imported here: pipeline imports clustering, and this module is only needed by the CLI.
    from .pipeline import _day, _group_key
    from .clustering import group_articles

    articles = read_articles(path)
    groups = group_articles(articles, _group_key, _day)
    joined = embeddings = None
    if embeddings_path is not None:
        embeddings = json.loads(Path(embeddings_path).read_text(encoding="utf-8"))
        vectors = group_vectors(groups, embeddings)
        if vectors is None:
            raise ValueError("The embeddings file does not match this corpus's groups; "
                             "pass a mapping of article ID to vector instead of a list")
        if isinstance(embeddings, list):
            embeddings = {a["article_id"]: vector for group, vector in zip(groups, embeddings) for a in group}
        joined = merge_same_event(groups, vectors)
    report = evaluate_grouping(articles, groups, read_authors(path), joined_groups=joined, embeddings=embeddings,
                               probes=probes)
    return json.dumps(report, indent=2, ensure_ascii=False) if as_json else render(report)
