"""Audit of the imported corpus against the original articles: is every figure really in its source?

For every fact of every story it re-reads the article from the feeds and checks that the cited evidence
is a verbatim passage of that article, that the figure is printed in that passage, that the article
belongs to the fact's story and has a link, and that a model-given measure name uses the article's own
words. It also checks every prepared narration: each number spoken must be the value of a cited fact.
It reads the feeds and the store only and makes no provider call.
"""
from __future__ import annotations

import re
from collections import Counter

from .pipeline import _NUMBER, _grounded_measure, _sentence_bounds, is_investment_advice
from .translations import valid_headline, valid_measure

_SPOKEN_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _digits(text: str) -> str:
    return re.sub(r"[.,]", "", text)


def audit(store, articles, summaries=None) -> dict:
    bodies = {str(article["article_id"]): str(article.get("body", "")) for article in articles}
    titles = {str(article["article_id"]): str(article.get("title", "")) for article in articles}
    stories = store.stories()
    totals = Counter()
    problems = []

    def problem(kind, story, fact=None):
        totals[kind] += 1
        if len(problems) < 20:
            problems.append({"problem": kind, "story": story["id"], **({"fact": fact["id"]} if fact else {})})

    facts_by_id = {}
    for story in stories:
        members = {str(article["article_id"]): article for article in story.get("articles", [])}
        totals["stories"] += 1
        totals["stories_withheld_as_advice"] += is_investment_advice(story)
        # A translated label may reword, never change a number: the headline keeps the title's numbers,
        # and a measure name holds none.
        for headline in (story.get("headlines") or {}).values():
            totals["translated_headlines"] += 1
            if not valid_headline(str(story.get("title", "")), headline):
                problem("translated_headline_changes_a_number", story)
        for fact in story.get("facts", []):
            for name in (fact.get("measure_translations") or {}).values():
                totals["translated_measure_names"] += 1
                if not valid_measure(str(fact.get("measure", "")), name):
                    problem("translated_measure_name_fails_its_check", story, fact)
            totals["facts"] += 1
            facts_by_id[str(fact["id"])] = fact
            article_id = str(fact.get("article_id", ""))
            body = bodies.get(article_id)
            evidence = str(fact.get("evidence", ""))
            number = _NUMBER.search(str(fact.get("value", "")))
            if article_id not in members:
                problem("fact_cites_an_article_outside_its_story", story, fact)
            elif not str(members[article_id].get("url", "")).startswith(("http://", "https://")):
                problem("source_without_a_link", story, fact)
            if body is None:
                problem("article_not_in_the_feeds", story, fact)
                continue
            position = body.find(evidence) if evidence else -1
            if position < 0:
                problem("evidence_is_not_a_verbatim_passage", story, fact)
                continue
            totals["evidence_verbatim"] += 1
            if not number or number.group(0) not in evidence:
                problem("figure_not_printed_in_its_evidence", story, fact)
            else:
                totals["figure_in_evidence"] += 1
            if fact.get("measure"):
                totals["measure_names"] += 1
                at = position + max(0, evidence.find(number.group(0))) if number else position
                left, right = _sentence_bounds(body, at)
                if _grounded_measure(str(fact["measure"]), body[left:right], f"{titles.get(article_id, '')} {body}"):
                    totals["measure_names_in_the_articles_words"] += 1
                else:
                    problem("measure_name_uses_words_the_article_does_not", story, fact)
    for story_id, summary in (summaries or {}).items():
        for variants in summary["variants"].values():
            for version, text in variants.items():
                # Each language cites its own facts: a figure is named in its article's language.
                citations = summary.get("segments", {}).get(version.split("/")[0]) or summary["segment"]
                cited = {_digits(_NUMBER.search(str(facts_by_id[fact_id]["value"])).group(0).lstrip("-−"))
                         for fact_id in citations.get("fact_ids", []) if fact_id in facts_by_id}
                totals["narrations"] += 1
                spoken = {_digits(number) for number in _SPOKEN_NUMBER.findall(text)}
                if spoken <= cited:
                    totals["narrations_with_only_cited_figures"] += 1
                else:
                    totals["narrations_with_an_uncited_figure"] += 1
    return {**{key: totals[key] for key in (
        "stories", "stories_withheld_as_advice", "facts", "evidence_verbatim", "figure_in_evidence", "measure_names",
        "measure_names_in_the_articles_words", "translated_headlines", "translated_measure_names", "narrations",
        "narrations_with_only_cited_figures", "narrations_with_an_uncited_figure")},
        "problems": {key: count for key, count in totals.items() if key in {
            "fact_cites_an_article_outside_its_story", "source_without_a_link", "article_not_in_the_feeds",
            "evidence_is_not_a_verbatim_passage", "figure_not_printed_in_its_evidence",
            "measure_name_uses_words_the_article_does_not", "translated_headline_changes_a_number",
            "translated_measure_name_fails_its_check"}},
        "examples": problems}
