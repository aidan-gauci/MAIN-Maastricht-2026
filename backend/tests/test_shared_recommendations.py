import shutil
import subprocess
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.app.audio import create_audio
from backend.app.config import DEMO
from backend.app.importer import read_articles
from backend.app.service import generate_briefing, prepare_corpus, render_audio
from backend.app.storage import Store
from backend.app.summaries import prepare_summaries


def profile(name, weights):
    return {
        "name": name, "topic_weights": weights, "language": "en", "expertise": "intermediate",
        "duration": 3,
    }


class SharedRecommendationTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        self.articles = read_articles(DEMO / "articles.jsonl")

    def test_different_listener_vectors_reuse_the_same_prepared_angle(self):
        prepare_corpus(self.store, self.articles)
        other_process = Store(self.store.path, self.store.audio_dir)
        with (
            patch("backend.app.summaries.narrate", side_effect=AssertionError("Request rendered a new summary")),
            patch("backend.app.providers.Providers.generate_script", side_effect=AssertionError("Request called a provider")),
            patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"),
        ):
            rates = generate_briefing(self.store, profile("First", {"rates": 0.9, "jobs": 0.1}))
            another = generate_briefing(other_process, profile("Second", {"interest rate": 0.65, "inflation": 0.35}))
            inflation = generate_briefing(self.store, profile("Third", {"inflation": 0.8, "rates": 0.2}))

        self.assertNotEqual(rates["id"], another["id"])
        self.assertEqual(rates["segments"][0], another["segments"][0])
        rate_segment = rates["segments"][0]
        inflation_segment = inflation["segments"][0]
        self.assertEqual(rate_segment["id"], inflation_segment["id"])
        self.assertIn("measures: policy rate: 3.25 %", rate_segment["text"])
        self.assertIn("measures: inflation: 2.6 %", inflation_segment["text"])
        self.assertEqual(
            {key: value for key, value in rate_segment.items() if key != "text"},
            {key: value for key, value in inflation_segment.items() if key != "text"},
        )

    def test_corpus_update_invalidates_a_warm_snapshot_across_workers(self):
        original = next(article for article in self.articles if article["article_id"] == "a02-en")
        prepare_corpus(self.store, [original])
        worker = Store(self.store.path, self.store.audio_dir)
        listener = profile("Listener", {"rates": 1})
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            before = generate_briefing(worker, listener)
            corrected = dict(original, body=original["body"].replace("3.25%", "3.50%"))
            prepare_corpus(self.store, [corrected])
            after = generate_briefing(worker, listener)

        self.assertEqual(before["stories"][0]["id"], after["stories"][0]["id"])
        self.assertIn("policy rate: 3.50 %", after["segments"][0]["text"])
        self.assertNotEqual(before["segments"][0]["fact_ids"], after["segments"][0]["fact_ids"])
        self.assertEqual(before, self.store.briefing(before["id"]))

    def test_existing_corpus_builds_one_shared_catalog_per_worker(self):
        stories = prepare_corpus(self.store, self.articles)
        self.store.replace_corpus(self.articles, stories)
        listener = profile("Listener", {"inflation": 1})

        with (
            patch("backend.app.service.prepare_summaries", wraps=prepare_summaries) as build,
            patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"),
        ):
            first = generate_briefing(self.store, listener)
            second = generate_briefing(self.store, listener)

        self.assertEqual(first["segments"], second["segments"])
        self.assertEqual(build.call_count, 1)


    def test_retry_audio_keeps_the_same_prepared_summary(self):
        prepare_corpus(self.store, self.articles)
        with patch("backend.app.service.create_audio", side_effect=RuntimeError("unavailable")):
            with self.assertLogs("backend.app.service", level="WARNING"):
                briefing = generate_briefing(self.store, profile("Listener", {"rates": 1}))
        self.assertEqual(briefing["audio_status"], "unavailable")
        original_segments = self.store.briefing(briefing["id"])["segments"]

        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            retried = render_audio(self.store, self.store.briefing(briefing["id"]))

        self.assertEqual(retried["audio_status"], "ready")
        self.assertEqual(retried["segments"], original_segments)


    def test_stale_audio_failure_cannot_replace_a_successful_retry(self):
        prepare_corpus(self.store, self.articles)
        with patch("backend.app.service.create_audio", side_effect=RuntimeError("unavailable")):
            with self.assertLogs("backend.app.service", level="WARNING"):
                briefing = generate_briefing(self.store, profile("Listener", {"rates": 1}))
        stale = self.store.briefing(briefing["id"])
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            ready = render_audio(self.store, briefing)
        with patch("backend.app.service.create_audio", side_effect=RuntimeError("second failure")):
            with self.assertLogs("backend.app.service", level="WARNING"):
                after = render_audio(self.store, stale)

        self.assertEqual(ready["audio_status"], "ready")
        self.assertEqual(after, ready)
        self.assertEqual(self.store.briefing(briefing["id"]), ready)

    def test_prepared_summary_formats_numeric_values_for_the_output_language(self):
        english = next(article for article in self.articles if article["article_id"] == "a02-en")
        prepare_corpus(self.store, [english])
        dutch = dict(profile("Listener", {"rates": 1}), language="nl")
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            briefing = generate_briefing(self.store, dutch)

        self.assertIn("3,25", briefing["segments"][0]["text"])
        self.assertIn("2,6", briefing["segments"][0]["text"])


class SharedAudioTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        self.first = {"id": "segment-first", "text": "First", "fact_ids": ["f1"]}
        self.second = {"id": "segment-second", "text": "Second", "fact_ids": ["f2"]}

    def render(self, segments, mode="demo", provider=None):
        return create_audio({"language": "en", "segments": segments}, self.store, provider, mode)

    @staticmethod
    def speak(text, path):
        frames = b"\x01\x00" * 8 if text == "First" else b"\x02\x00" * 16
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(frames)

    def test_shared_clips_are_synthesized_once_and_assembled_in_rank_order(self):
        with patch("backend.app.audio.offline_voice", side_effect=self.speak) as voice:
            original = self.render([self.first, self.second])
            repeated = self.render([self.first, self.second])
            reversed_url = self.render([self.second, self.first])
            revised = self.render([dict(self.first, fact_ids=["revised"]), self.second])

        self.assertEqual(original, repeated)
        self.assertNotEqual(original, reversed_url)
        self.assertNotEqual(original, revised)
        self.assertEqual(voice.call_count, 3)
        self.assertEqual(self.store.usage("demo")[0]["calls"], 3)
        for url, samples in (
            (original, b"\x01\x00" * 8 + b"\x02\x00" * 16),
            (reversed_url, b"\x02\x00" * 16 + b"\x01\x00" * 8),
        ):
            with wave.open(str(self.store.audio_dir / url.rsplit("/", 1)[-1]), "rb") as audio:
                self.assertEqual(audio.readframes(audio.getnframes()), samples)

    def test_concurrent_requests_share_audio_without_partial_files(self):
        with (
            patch("backend.app.audio.offline_voice", side_effect=self.speak) as voice,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(pool.map(self.render, [[self.first, self.second]] * 24))

        self.assertEqual(len(set(results)), 1)
        self.assertEqual(voice.call_count, 2)
        self.assertFalse(list(self.store.audio_dir.glob("*.tmp.*")))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for the live assembly check")
    def test_shared_mp3_clips_are_assembled_without_another_provider_call(self):
        def synthesize(_, path, language):
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                 "-i", "anullsrc=r=44100:cl=mono", "-t", "0.1", "-b:a", "128k", str(path)],
                check=True, capture_output=True, timeout=10,
            )

        provider = Mock(synthesize=Mock(side_effect=synthesize))
        first = self.render([self.first, self.second], "live", provider)
        second = self.render([self.second, self.first], "live", provider)
        self.assertNotEqual(first, second)
        self.assertEqual(provider.synthesize.call_count, 2)
        for url in (first, second):
            output = self.store.audio_dir / url.rsplit("/", 1)[-1]
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(output), "-f", "null", "-"],
                check=True, capture_output=True, timeout=10,
            )
