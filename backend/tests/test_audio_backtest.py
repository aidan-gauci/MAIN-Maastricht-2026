import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app.audio_backtest import run
from backend.app.config import DEMO
from backend.app.importer import read_articles
from backend.app.service import prepare_corpus
from backend.app.storage import Store


class AudioBacktestTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))

    def test_backtest_plans_without_speaking_and_sharing_grows_with_the_audience(self):
        with patch("backend.app.audio.offline_voice", side_effect=AssertionError("backtest must not synthesize")), \
                patch("backend.app.providers.Providers.synthesize", side_effect=AssertionError("backtest must not synthesize")):
            report = run(self.store, audiences=(20, 200), days=2, seed=3)
        small, large = report["audiences"]
        for row in (small, large):
            self.assertEqual(row["facts_replayed_as_news"], 0)
            self.assertEqual(row["facts_without_their_source"], 0)
            self.assertLessEqual(row["clips_rendered"], row["clips_requested"])
            self.assertLess(row["cost_usd"], row["cost_usd_without_sharing"])
        self.assertGreater(large["clips_reused_percent"], small["clips_reused_percent"])
        self.assertLess(large["cost_per_listener_per_month_usd"], small["cost_per_listener_per_month_usd"])
        self.assertFalse(list(self.store.audio_dir.glob("*")))   # nothing was rendered
        self.assertEqual(run(self.store, audiences=(20,), days=2, seed=3)["audiences"][0], small)  # reproducible


if __name__ == "__main__":
    unittest.main()
