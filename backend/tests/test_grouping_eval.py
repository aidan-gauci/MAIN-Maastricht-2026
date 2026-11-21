"""The offline grouping evaluation, on a tiny synthetic feed in the organizers' raw format."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.clustering import group_articles
from backend.app.grouping_eval import evaluate_grouping, group_vectors, render, run_evaluation, translation_truth
from backend.app.importer import read_articles, read_authors
from backend.app.pipeline import _day, _group_key

ROOT = Path(__file__).resolve().parents[2]
EN_BODY = "Revenue was 4.2 billion euros. Profit was 610 million euros. Growth was 3.5 percent."
DE_BODY = "Der Umsatz betrug 4,2 Milliarden Euro. Der Gewinn lag bei 610 Millionen Euro. Das Wachstum lag bei 3,5 Prozent."
OTHER_BODY = "Index futures rose 0.8% while the 10-year yield eased to 4.12%. Payrolls added 175,000 jobs."


def raw(identifier, title, date, language, author, outlet, body):
    return {"$id": identifier, "headline": title, "date": date, "language": language, "source": author,
            "publication": {"name": outlet}, "text_content": body, "permalink": f"https://example.invalid/{identifier}",
            "company_tickers": []}


ROWS = [
    raw("en1", "Zorbo reports revenue", "2026-11-16T08:00:00Z", "en", "Ada", "Invezz", EN_BODY),
    raw("de1", "Zorbo meldet Umsatz", "2026-11-16T08:02:00Z", "de", "Ada", "Invezz - German", DE_BODY),
    raw("en2", "Markets steady", "2026-11-16T08:05:00Z", "en", "Ada", "Invezz", OTHER_BODY),
    raw("nl1", "Zorbo meldt omzet", "2026-11-16T08:01:00Z", "nl", "Ada", "Invezz - Dutch", DE_BODY),
    raw("en3", "Quux wins contract", "2026-11-16T12:00:00Z", "en", "Team", "Invezz", EN_BODY.replace("610", "611")),
    raw("de3", "Quux gewinnt Auftrag", "2026-11-16T12:03:00Z", "de", "Bee", "Invezz - German", EN_BODY.replace("610", "611")),
]


class GroupingEvaluationTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.feed = self.root / "feed.jsonl"
        self.feed.write_text("\n".join(json.dumps(row) for row in ROWS), encoding="utf-8")

    def test_authors_are_read_from_the_raw_feed(self):
        self.assertEqual({"en1": "Ada", "de1": "Ada", "en2": "Ada", "nl1": "Ada", "en3": "Team", "de3": "Bee"},
                         read_authors(self.feed))

    def test_ground_truth_is_the_nearest_english_piece_by_the_same_author_or_else_by_figures(self):
        articles, how = read_articles(self.feed), {}
        truth = translation_truth(articles, read_authors(self.feed), how)
        # Ada has two English pieces within 15 minutes: each translation belongs to the nearer, en1.
        self.assertEqual({"en1"}, truth["de1"])
        self.assertEqual({"en1"}, truth["nl1"])
        # A different byline still counts when the same outlet's English article shares most figures.
        self.assertEqual({"en3"}, truth["de3"])
        self.assertEqual({"author": 2, "figures": 1}, {k: list(how.values()).count(k) for k in ("author", "figures")})

    def test_the_report_scores_recall_and_precision_and_lists_titles_only(self):
        articles = read_articles(self.feed)
        groups = group_articles(articles, _group_key, _day)
        report = evaluate_grouping(articles, groups, read_authors(self.feed))
        translation = report["translation"]
        self.assertEqual((3, 3, 1.0, 1.0), (translation["truth_articles"], translation["recalled"],
                                            translation["recall"], translation["pair_precision"]))
        self.assertEqual([], translation["false_joins"])
        self.assertEqual({1: 1, 2: 1, 3: 1}, report["size_distribution"])
        text = render(report)
        self.assertIn("3/3 recalled", text)
        self.assertNotIn("Revenue was", text)       # article text never appears

    def test_event_threads_are_reported_with_probes_for_a_named_event(self):
        saga = [raw(f"x{i}", title, f"2026-11-1{i}T09:00:00Z", "en", "Cy", "IBTimes", f"Shares moved {i}1 percent on the news.")
                for i, title in enumerate(("Zorbo steps down as Acme chief tonight", "Acme chief Zorbo steps down early"), 3)]
        self.feed.write_text("\n".join(json.dumps(row) for row in ROWS + saga), encoding="utf-8")
        text = run_evaluation(self.feed, probes=["Zorbo", "Quux"])
        self.assertIn("event threads 1 holding 2 stories", text)
        self.assertIn("probe 'Zorbo': 2 of 3 stories in a thread", text)      # the third is the quarterly report
        self.assertIn("probe 'Quux': 0 of 1 stories in a thread", text)

    def test_a_wrong_join_is_reported(self):
        articles = read_articles(self.feed)
        groups = group_articles(articles, _group_key, _day)
        everything = [[a for group in groups for a in group]]
        report = evaluate_grouping(articles, everything, read_authors(self.feed))
        self.assertLess(report["translation"]["pair_precision"], 1.0)
        self.assertTrue(report["translation"]["false_joins"])
        self.assertEqual(1, report["groups"])

    def test_embeddings_by_article_give_each_group_a_vector_and_a_list_must_match(self):
        articles = read_articles(self.feed)
        groups = group_articles(articles, _group_key, _day)
        vectors = group_vectors(groups, {"en1": [1.0, 0.0], "en3": [0.0, 1.0]})
        self.assertEqual(len(groups), len(vectors))
        self.assertIn([1.0, 0.0], vectors)
        self.assertIsNone(group_vectors(groups, [[1.0]]))

    def test_the_command_runs_offline_and_without_a_store(self):
        embeddings = self.root / "vectors.json"
        embeddings.write_text(json.dumps({"en1": [1.0, 0.0], "en3": [1.0, 0.0]}), encoding="utf-8")
        text = run_evaluation(self.feed, embeddings)
        self.assertIn("cross-outlet merges", text)
        self.assertIn("translations: 3/3 recalled", text)
        env = {key: value for key, value in os.environ.items() if "API_KEY" not in key}
        env.update(BRIEFING_DATA_DIR=str(self.root / "data"))
        result = subprocess.run([sys.executable, "-m", "backend.app", "evaluate-grouping", str(self.feed), "--json"],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1.0, json.loads(result.stdout)["translation"]["recall"])
        self.assertFalse((self.root / "data").exists())     # no database was created


if __name__ == "__main__":
    unittest.main()
