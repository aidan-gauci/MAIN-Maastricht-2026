from copy import deepcopy
from itertools import permutations
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from backend.app.config import DEMO, Wildcard
from backend.app.importer import read_articles
from backend.app.pipeline import fixture_articles, narrate, prepare, rank
from backend.app.service import generate_briefing, prepare_corpus, wildcard_profile
from backend.app.storage import Store


def article(key, title, body, **fields):
    return {
        "article_id": key, "source": f"{key} Journal", "url": f"https://{key}.example/story",
        "publication_date": "2026-11-19", "language": "en", "title": title, "body": body,
        **fields,
    }


def members(story):
    return {row["article_id"] for row in story["articles"]}


class GroupingQualityTest(unittest.TestCase):
    def test_multilingual_duplicates_on_distinct_urls(self):
        editions = fixture_articles()[:3]
        ids = {a["article_id"] for a in editions}
        story_ids = set()
        for order in permutations(editions):
            story, = prepare(list(order), None)
            self.assertEqual(members(story), ids)
            story_ids.add(story["id"])
        self.assertEqual(len(story_ids), 1)

    def test_numbers_dates_and_boilerplate_do_not_identify_an_event(self):
        base = article("first", "Arden reports Q1 quarterly revenue of 4.2 billion euros",
                       "Arden reported revenue of 4.2 billion euros.")
        for change in (
            {"title": base["title"].replace("Arden", "Ilex")},
            {"title": base["title"].replace("revenue", "profit")},
            {"title": base["title"].replace("Q1", "Q2")},
            {"publication_date": "2026-12-18"},
            {"tickers": ["ILEX"]},
        ):
            with self.subTest(change=change):
                first = dict(base, tickers=["ARD"])
                second = dict(base, article_id="second", url="https://wire.example/second", **change)
                stories = prepare([first, second], None)
                self.assertEqual([members(s) for s in stories], [{"first"}, {"second"}])

    def test_generic_reporting_verbs_do_not_link_different_companies(self):
        first = article("arden", "Arden posts quarterly revenue of 4.2 billion euros",
                        "Arden posted revenue of 4.2 billion euros.")
        second = article("ilex", "Ilex posts quarterly revenue of 4.2 billion euros",
                         "Ilex posted revenue of 4.2 billion euros.")
        self.assertEqual([members(s) for s in prepare([first, second], None)],
                         [{"arden"}, {"ilex"}])

    def test_distinct_articles_on_a_shared_url_path(self):
        for urls in (("https://wire.example/news", "https://wire.example/news"),
                     ("https://wire.example/read?id=11", "https://wire.example/read?id=12")):
            with self.subTest(urls=urls):
                rows = [article("rail", "Port rail extension", "Freight capacity rose by 9 percent.", url=urls[0]),
                        article("farm", "Orchard prices", "Food prices rose by 2 percent.", url=urls[1])]
                self.assertEqual(len(prepare(rows, None)), 2)

    def test_disagreeing_sources_share_one_conflict_and_keep_both_values(self):
        editions = fixture_articles()[:3]
        story, = prepare(editions, None)
        conflict, = story["conflicts"]
        facts = [f for f in story["facts"] if f["conflict_status"] == "conflict"]
        self.assertEqual({f["article_id"]: f["value"] for f in facts},
                         {"fx-en-1": "610", "fx-de-1": "590", "fx-nl-1": "610"})
        self.assertEqual(conflict["fact_ids"], sorted(f["id"] for f in facts))
        self.assertEqual({f["conflict_group"] for f in facts}, {conflict["id"]})
        self.assertEqual(len([f for f in story["facts"] if f["conflict_status"] == "supported"]), 3)
        self.assertEqual(prepare(editions[::-1], None)[0]["conflicts"], story["conflicts"])

    def test_different_currencies_or_reporting_periods_are_not_competing_profit_estimates(self):
        english, german = fixture_articles()[:2]
        for body in (
            "Der Betriebsgewinn betrug 590 Millionen Dollar.",
            "Der Betriebsgewinn im Vorjahr betrug 590 Millionen Euro.",
        ):
            with self.subTest(body=body):
                german = dict(german, body=body)
                story, = prepare([english, german], None)
                self.assertTrue(any(f["article_id"] == german["article_id"] for f in story["facts"]))
                self.assertFalse(story["conflicts"])

    def test_values_from_different_periods_in_the_same_language_are_not_conflicts(self):
        title = "Arden reports operating profit"
        for current, previous in (("this year", "last year"), ("in 2026", "in 2025")):
            with self.subTest(current=current, previous=previous):
                first = article("first", title, f"Operating profit was 610 million euros {current}.")
                second = article("second", title, f"Operating profit was 590 million euros {previous}.")
                story, = prepare([first, second], None)
                self.assertEqual({f["value"] for f in story["facts"]}, {"610", "590"})
                self.assertFalse(story["conflicts"])


class RecommendationQualityTest(unittest.TestCase):
    def setUp(self):
        self.stories = prepare([
            article("energy", "Solar auction closes", "Solar capacity rose by 12 megawatts.",
                    topics=["Renewable energy"]),
            article("jobs", "Workers approve wage deal", "The wage increase covers 80 workers."),
            article("export", "Exports rise", "Exports rose by 3 percent.", topics=["Customs"]),
        ], None)

    def test_exclusions_override_weights_and_use_whole_topic_names(self):
        profile = {"excluded_topics": ["Employment", "customs", "transport"],
                   "topic_weights": {"jobs": 1, "trade": 1}}
        self.assertEqual([members(s) for s in rank(self.stories, profile)], [{"energy"}])
        # "port" in exports and "art" in an explicit tag must not trigger unrelated exclusions.
        self.stories[-1]["topics"].append("cartography")
        allowed = rank(self.stories, {"excluded_topics": ["transport", "art"]})
        self.assertIn({"export"}, [members(s) for s in allowed])

    def test_profiles_change_the_first_story_and_explain_the_match(self):
        for interest, expected in (("energy", "energy"), ("employment", "jobs"), ("customs", "export")):
            with self.subTest(interest=interest):
                first = rank(self.stories, {"topic_weights": {interest: 1}})[0]
                self.assertEqual(members(first), {expected})
                self.assertIn(interest, first["reason"])

    def test_evidence_match_can_explain_a_story_with_older_topic_labels(self):
        older = dict(self.stories[0], topics=["news"])
        story, = rank([older], {"topic_weights": {"energy": 1}})
        self.assertIn("energy", story["reason"])
        self.assertEqual(story["score_components"]["topic_match"], 0)
        self.assertEqual(story["score_components"]["relevance"], 1)

    def test_weighted_priorities_change_order_and_exclusion_wins_for_older_stories(self):
        for preferred, secondary in (("energy", "jobs"), ("jobs", "energy")):
            ranked = rank(self.stories, {"topic_weights": {preferred: 0.8, secondary: 0.2}})
            self.assertEqual([members(s) for s in ranked[:2]], [{preferred}, {secondary}])
        older = dict(self.stories[0], topics=["news"])
        self.assertFalse(rank([older], {"excluded_topics": ["energy"], "topic_weights": {"energy": 1}}))

    def test_only_sourced_candidates_can_affect_scores_or_fill_a_slot(self):
        original = self.stories[0]
        for field, changes in (
            ("facts", {"article_id": "unknown"}), ("facts", {"evidence": ""}),
            ("facts", {"value": "99"}), ("facts", {"value": "12.000000001"}),
            ("facts", {"unit": "days"}),
            ("articles", {"source": "  "}), ("articles", {"url": "file:///story"}),
            ("articles", {"url": "https://[invalid"}),
        ):
            with self.subTest(field=field, changes=changes):
                invalid = deepcopy(original)
                invalid.update(id="invalid", publication_date="2026-11-28")
                invalid[field][0].update(changes)
                profile = {"topic_weights": {"energy": 1}}
                self.assertEqual(rank([original, invalid], profile), rank([original], profile))
        self.assertFalse(rank([dict(original, facts=[])], {}))

    def test_profiles_preserve_every_fact_conflict_and_citation(self):
        articles = fixture_articles()
        bodies = {row["article_id"]: row["body"] for row in articles}
        stories = prepare(articles, None)
        snapshot = deepcopy(stories)
        by_id = {story["id"]: story for story in snapshot}
        for topic, language in (("rates", "en"), ("earnings", "nl")):
            profile = {"topic_weights": {topic: 1}, "language": language, "duration": 5}
            ranked = rank(stories, profile)
            self.assertEqual({row["id"] for row in ranked}, set(by_id))
            segments = {s["id"]: s for s in narrate(ranked, profile, None)}
            for row in ranked:
                source = by_id[row["id"]]
                for key, value in source.items():
                    self.assertEqual(row[key], value)
                segment = segments["segment-" + row["id"]]
                self.assertEqual(segment["fact_ids"], [f["id"] for f in source["facts"]])
                self.assertEqual(segment["sources"], source["articles"])
                self.assertEqual(segment["conflict"], bool(source["conflicts"]))
                self.assertEqual(segment["conflict_labels"], list(dict.fromkeys(
                    conflict["label"] for conflict in source["conflicts"]
                )))
                for fact in source["facts"]:
                    self.assertIn(fact["evidence"], bodies[fact["article_id"]])
                    if fact["conflict_status"] == "conflict":
                        self.assertIn(fact["value"], segment["text"])
        self.assertEqual(stories, snapshot)


class SummaryPersonalizationTest(unittest.TestCase):
    def test_wildcard_profile_selects_and_emphasizes_the_same_story_facts(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = Store(root / "test.sqlite3", root / "audio")
        store.initialize()
        stories = prepare_corpus(store, read_articles(DEMO / "articles.jsonl"))
        bellmare = next(story for story in stories if "a02-en" in members(story))

        segments = {}
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            for topic, lead in (("rates", "policy rate: 3.25 %"), ("inflation", "inflation: 2.6 %")):
                with self.subTest(topic=topic):
                    profile = wildcard_profile(Wildcard(
                        topics=[topic], expertise="intermediate", language="en", duration=3,
                    ))
                    briefing = generate_briefing(store, profile)
                    first_story = briefing["stories"][0]
                    self.assertEqual(first_story["id"], bellmare["id"])
                    self.assertIn(topic, first_story["reason"])
                    segment = briefing["segments"][0]
                    self.assertEqual(segment["id"], "segment-" + bellmare["id"])
                    self.assertTrue(segment["text"].startswith(f"The reports give these measures: {lead};"))
                    self.assertIn("policy rate: 3.25 %", segment["text"])
                    self.assertIn("inflation: 2.6 %", segment["text"])
                    segments[topic] = {key: value for key, value in segment.items() if key != "text"}

        self.assertEqual(segments["rates"], segments["inflation"])
        self.assertEqual(segments["rates"]["fact_ids"], [fact["id"] for fact in bellmare["facts"]])
        self.assertEqual(segments["rates"]["sources"], bellmare["articles"])
        self.assertEqual({story["id"]: story for story in store.stories()},
                         {story["id"]: story for story in stories})

    def test_live_summary_uses_weighted_emphasis_and_falls_back_to_complete_figures(self):
        story, = prepare([
            article("bank", "Central bank leaves policy rate on hold",
                    "The bank kept its policy rate at 3.25 percent. It forecasts inflation of 2.6 percent."),
        ], None)
        profile = {
            "topic_weights": {"rates": 0.2, "inflation": 0.8},
            "expertise": "intermediate", "language": "en",
        }
        ranked = rank([story], profile)
        offline, = narrate(ranked, profile, None)
        valid = "Projected inflation is 2.6 percent. The policy rate remains at 3.25 percent."
        provider = Mock(live=True)
        provider.generate_script.return_value = valid

        live, = narrate(ranked, profile, provider)
        self.assertEqual(live, dict(offline, text=valid))
        prompt, language = provider.generate_script.call_args.args
        self.assertEqual(language, "en")
        self.assertLess(prompt.index("verified amount=2.6"), prompt.index("verified amount=3.25"))

        for candidate in (
            "Projected inflation is 2.6 percent.",
            "The policy rate remains at 3.25 percent. Projected inflation is 2.6 percent.",
            "Projected inflation is 2.6 percent. The policy rate remains at 3.75 percent.",
        ):
            with self.subTest(candidate=candidate):
                provider.generate_script.return_value = candidate
                self.assertEqual(narrate(ranked, profile, provider), [offline])
