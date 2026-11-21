import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.claim_audit import audit
from backend.app.config import DEMO
from backend.app.importer import read_articles
from backend.app.service import prepare_corpus
from backend.app.storage import Store


class ClaimAuditTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        self.articles = read_articles(DEMO / "articles.jsonl")
        prepare_corpus(self.store, self.articles)
        self.summaries = self.store.recommendation_snapshot(lambda stories, summaries: summaries)

    def test_every_fixture_figure_and_narration_traces_to_its_article(self):
        report = audit(self.store, self.articles, self.summaries)
        self.assertGreater(report["facts"], 0)
        self.assertEqual(report["evidence_verbatim"], report["facts"])
        self.assertEqual(report["figure_in_evidence"], report["facts"])
        self.assertEqual(report["narrations_with_only_cited_figures"], report["narrations"])
        self.assertEqual(report["problems"], {})

    def test_an_altered_article_or_an_invented_spoken_figure_is_reported(self):
        altered = [dict(article, body=article["body"].replace("3.25", "3.50")) for article in self.articles]
        report = audit(self.store, altered, self.summaries)
        self.assertGreater(report["problems"]["evidence_is_not_a_verbatim_passage"], 0)

        story_id, summary = next(iter(self.summaries.items()))
        invented = {story_id: {**summary, "variants": {"": {"en/intermediate": "Policy rate, 99.9 percent."}}}}
        self.assertEqual(audit(self.store, self.articles, invented)["narrations_with_an_uncited_figure"], 1)


if __name__ == "__main__":
    unittest.main()
