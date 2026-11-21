import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app import service
from backend.app.config import DEMO
from backend.app.importer import read_articles
from backend.app.service import generate_briefing, prepare_corpus
from backend.app.storage import Store


def profile():
    return {"name": "Markets", "topic_weights": {"markets": 1.0}, "language": "en", "expertise": "intermediate", "duration": 3}


class SharedReturningListenerTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))

    def test_returning_listeners_in_the_same_state_get_identical_text_without_a_model_call(self):
        listeners = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
        with patch.object(service, "create_audio", return_value="/api/audio/test.wav"), \
                patch.object(service, "audio_file", return_value=None):
            for listener in listeners:  # first briefing: shared summaries, facts recorded as heard
                generate_briefing(self.store, profile(), listener_id=listener)
                self.assertTrue(self.store.spoken_fact_ids(listener, "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"))
            with patch.object(service, "MODE", "live"), \
                    patch("backend.app.providers.Providers.generate_script", side_effect=AssertionError("per-listener model call")):
                second = [generate_briefing(self.store, profile(), listener_id=listener,
                                            spoken_fact_ids=self.store.spoken_fact_ids(listener, "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"))
                          for listener in listeners]
        texts = [[segment["text"] for segment in briefing["segments"]] for briefing in second]
        self.assertTrue(texts[0])
        self.assertEqual(texts[0], texts[1])


if __name__ == "__main__":
    unittest.main()
