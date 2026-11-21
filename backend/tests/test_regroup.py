"""`regroup` rebuilds stored stories under the current rules from stored facts, with no provider call."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app import pipeline
from backend.app.regroup import regroup_corpus
from backend.app.storage import Store
from backend.app.summaries import prepare_summaries

ROOT = Path(__file__).resolve().parents[2]


def article(article_id, language, title, body, published, source="Invezz"):
    return {"article_id": article_id, "language": language, "title": title, "body": body, "source": source,
            "url": f"https://example.invalid/{language}/{article_id}", "publication_date": published,
            "official": False, "tickers": []}


REPORT = [
    article("en", "en", "Zorbo reports quarterly revenue of 4.2 billion euros",
            "Zorbo reported revenue of 4.2 billion euros for the quarter. Its release said operating profit was "
            "610 million euros. The company expects growth of 3.5 percent.", "2026-11-16T08:00:00Z"),
    article("de", "de", "Zorbo meldet Quartalsumsatz von 4,2 Milliarden Euro",
            "Zorbo meldete einen Quartalsumsatz von 4,2 Milliarden Euro. Der Gewinn aus dem Betrieb betrug laut "
            "Mitteilung 590 Millionen Euro. Das Unternehmen erwartet ein Wachstum von 3,5 Prozent.",
            "2026-11-16T08:02:00Z", "Invezz - German"),
    article("nl", "nl", "Zorbo: kwartaalomzet 4,2 miljard euro",
            "Zorbo meldde een kwartaalomzet van 4,2 miljard euro. De operationele winst was volgens het bericht "
            "610 miljoen euro. Het bedrijf verwacht een groei van 3,5 procent.",
            "2026-11-16T08:03:00Z", "Invezz - Dutch"),
]

ANALYSIS = "Zorbo crude analysis: will oil crash below 90 as supply glut grows"
OUTLOOK = "Zorbo price outlook: will oil crash below 90 as supply glut grows"
FOLLOW_UPS = [
    article("mon", "en", ANALYSIS, "Oil prices rose 11 percent and 12 percent. Exports fell 13 percent, 14 percent and "
            "15 percent. Growth was 16 percent, 17 percent and 18 percent.", "2026-11-16T08:00:00Z"),
    article("tue", "en", OUTLOOK, "Oil prices rose 11 percent and 12 percent. Exports fell 13 percent, 14 percent and "
            "15 percent. Growth was 41 percent, 42 percent and 43 percent.", "2026-11-17T07:00:00Z"),
]


def singleton_stories(articles):
    """Stories as an import that kept every article apart would have stored them."""
    return [story for item in articles for story in pipeline._offline_extract([item])]


class RegroupTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()

    def seed(self, articles, stories):
        self.store.replace_corpus(articles, stories, prepare_summaries(stories))

    def test_translations_stored_apart_are_joined_with_their_facts_and_conflicts(self):
        self.seed(REPORT, singleton_stories(REPORT))
        summary = regroup_corpus(self.store, REPORT)
        story, = self.store.stories()
        fresh, = pipeline._offline_extract(REPORT)
        self.assertEqual((3, 1, 1), (summary["stories_before"], summary["stories_after"], summary["merged_stories"]))
        self.assertEqual({"en", "de", "nl"}, {a["language"] for a in story["articles"]})
        self.assertEqual(sorted(f["id"] for f in fresh["facts"]), sorted(f["id"] for f in story["facts"]))
        self.assertTrue(fresh["conflicts"], "the fixture of this test must hold a conflict")
        self.assertEqual(fresh["conflicts"], story["conflicts"])
        self.assertEqual({f["id"] for f in fresh["facts"] if f["conflict_status"] == "conflict"},
                         {f["id"] for f in story["facts"] if f["conflict_status"] == "conflict"})
        self.assertEqual("Zorbo reports quarterly revenue of 4.2 billion euros", story["title"])
        self.assertEqual("2026-11-16", story["publication_date"])
        self.assertEqual(pipeline._story_id(["en", "de", "nl"]), story["id"])
        self.assertTrue(all("_claim_group" not in f for f in story["facts"]))
        self.assertIn(story["id"], self.store.recommendation_snapshot(lambda *args: args)[1])

    def test_conflict_labels_of_the_old_members_do_not_outlive_them(self):
        english_and_german, = pipeline._offline_extract(REPORT[:2])
        dutch, = pipeline._offline_extract(REPORT[2:])
        self.assertTrue(english_and_german["conflicts"])
        self.seed(REPORT, [english_and_german, dutch])
        regroup_corpus(self.store, REPORT)
        story, = self.store.stories()
        labelled = {f["conflict_group"] for f in story["facts"] if f["conflict_status"] == "conflict"}
        self.assertNotEqual(english_and_german["conflicts"], story["conflicts"])
        self.assertEqual({c["id"] for c in story["conflicts"]}, labelled)
        # The Dutch report repeats the English figure, so it joins the competing claims.
        self.assertTrue(any(f["article_id"] == "nl" and f["conflict_status"] == "conflict" for f in story["facts"]))

    def test_a_story_that_fused_two_days_of_one_outlet_is_split_but_stays_in_one_thread(self):
        first, second = singleton_stories(FOLLOW_UPS)
        fused = dict(first, id=pipeline._story_id(["mon", "tue"]), articles=first["articles"] + second["articles"],
                     facts=first["facts"] + second["facts"])
        self.seed(FOLLOW_UPS, [fused])
        summary = regroup_corpus(self.store, FOLLOW_UPS)
        stories = self.store.stories()
        self.assertEqual((1, 2, 1), (summary["stories_before"], summary["stories_after"], summary["split_stories"]))
        for story in stories:
            members = {a["article_id"] for a in story["articles"]}
            self.assertEqual(1, len(members))
            self.assertTrue(all(f["article_id"] in members for f in story["facts"]))
        self.assertEqual(sorted(f["id"] for f in first["facts"] + second["facts"]),
                         sorted(f["id"] for s in stories for f in s["facts"]))
        self.assertEqual(1, summary["threads"])
        self.assertEqual(1, len({s["thread_id"] for s in stories}))
        self.assertTrue(all(s["thread_title"] for s in stories))

    def test_regrouping_an_unchanged_corpus_changes_nothing_and_is_repeatable(self):
        self.seed(REPORT, pipeline._offline_extract(REPORT))
        before = self.store.stories()
        summary = regroup_corpus(self.store, REPORT)
        self.assertEqual(before, self.store.stories())
        self.assertEqual((1, 1, 1, 0, 0), (summary["stories_before"], summary["stories_after"], summary["unchanged"],
                                           summary["merged_stories"], summary["split_stories"]))
        regroup_corpus(self.store, REPORT)
        self.assertEqual(before, self.store.stories())

    def test_stored_story_embeddings_merge_cross_outlet_reports_again(self):
        body = ("Revenue was 11 percent, profit was 12 percent and growth was 13 percent. "
                "Exports fell 14 percent, prices rose 15 percent and wages grew 16 percent.")
        articles = [article("a", "en", "Zorbo posts results", body, "2026-11-16T08:00:00Z", "Invezz"),
                    article("b", "en", "Acme reports numbers", body, "2026-11-16T09:00:00Z", "IBTimes")]
        for second, expected in (([0.95, 0.05], 1), ([0.0, 1.0], 2)):
            with self.subTest(second=second):
                stories = singleton_stories(articles)
                stories[0]["embedding"], stories[1]["embedding"] = [1.0, 0.0], second
                self.seed(articles, stories)
                summary = regroup_corpus(self.store, articles)
                self.assertEqual(expected, summary["stories_after"])
                self.assertEqual(2 - expected, summary["cross_outlet_merges"])
                self.assertTrue(summary["used_story_embeddings"])
                self.assertTrue(all(s.get("embedding") for s in self.store.stories()))

    def test_it_needs_the_imported_feeds_and_keeps_the_previous_corpus_on_any_failure(self):
        self.seed(REPORT, singleton_stories(REPORT))
        before = self.store.stories()
        with self.assertRaisesRegex(ValueError, "missing 1 imported"):
            regroup_corpus(self.store, REPORT[:2])
        with patch("backend.app.regroup.prepare_summaries", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                regroup_corpus(self.store, REPORT)
        self.assertEqual(before, self.store.stories())

    def test_an_empty_store_has_nothing_to_regroup(self):
        with self.assertRaisesRegex(ValueError, "Nothing to regroup"):
            regroup_corpus(self.store, REPORT)

    def test_no_provider_is_ever_created(self):
        self.seed(REPORT, singleton_stories(REPORT))
        with patch("backend.app.providers.Providers.__init__", side_effect=AssertionError("provider created")):
            regroup_corpus(self.store, REPORT)

    def test_usage_recorded_by_the_import_is_left_alone(self):
        self.store.record_usage("story_extraction", mode="live", input_tokens=10)
        self.seed(REPORT, singleton_stories(REPORT))
        regroup_corpus(self.store, REPORT)
        self.assertEqual([("story_extraction", 10)],
                         [(row["stage"], row["input_tokens"]) for row in self.store.usage("live")])


class RegroupCliTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "data"

    def cli(self, *args, corpus="fixture"):
        env = {key: value for key, value in os.environ.items() if "API_KEY" not in key and key != "ALLOW_EXTERNAL_PROVIDER_DATA"}
        env.update(BRIEFING_DATA_DIR=str(self.data), BRIEFING_CORPUS=corpus, BRIEFING_MODE="demo")
        return subprocess.run([sys.executable, "-m", "backend.app", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_regroup_after_import_reports_a_summary_and_keeps_the_fixture_intact(self):
        self.assertEqual(0, self.cli("import").returncode)
        directory = self.data / "fixture" / "demo"
        stored = Store(directory / "briefings.sqlite3", directory / "audio")
        imported = stored.stories()
        result = self.cli("regroup")
        self.assertEqual(0, result.returncode, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["stories_before"], summary["stories_after"])
        self.assertEqual(summary["stories_before"], summary["unchanged"])
        self.assertEqual((0, 0), (summary["merged_stories"], summary["split_stories"]))
        self.assertEqual(("demo", "fixture"), (summary["mode"], summary["corpus"]))
        # The fixture's stories, with their translations grouped, come out exactly as imported.
        self.assertEqual(imported, stored.stories())

    def test_regroup_follows_the_import_rules_for_paths_and_empty_corpora(self):
        outside = self.data / "feed.jsonl"
        outside.parent.mkdir(parents=True)
        outside.write_text((ROOT / "backend" / "demo" / "articles.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        refused = self.cli("regroup", str(outside))
        self.assertEqual(2, refused.returncode)
        self.assertIn("BRIEFING_CORPUS=event", refused.stderr)
        empty = self.cli("regroup")
        self.assertEqual(2, empty.returncode)
        self.assertIn("Nothing to regroup", empty.stderr)
        self.assertNotIn("Traceback", empty.stderr)
        self.assertEqual(0, self.cli("import", str(outside), corpus="event").returncode)
        self.assertEqual(0, self.cli("regroup", str(outside), corpus="event").returncode)


if __name__ == "__main__":
    unittest.main()
