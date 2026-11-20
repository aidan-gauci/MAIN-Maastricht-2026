"""Demo-readiness checks: credential-free fixture, audio recovery, cost separation, import and cleanup."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from backend.app import main, service
from backend.app.config import DEMO, ROOT, load_profiles
from backend.app.importer import read_articles
from backend.app.providers import Providers
from backend.app.service import generate_briefing, prepare_corpus, usage_report
from backend.app.storage import Store

PROVIDER_ENV = (
    "OPENAI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ALLOW_EXTERNAL_PROVIDER_DATA",
    "OPENAI_INPUT_USD_PER_MILLION_TOKENS", "OPENAI_OUTPUT_USD_PER_MILLION_TOKENS",
    "OPENAI_EMBEDDING_INPUT_USD_PER_MILLION_TOKENS", "ELEVENLABS_USD_PER_1000_CHARS",
)


def no_provider_env():
    """Run with no provider credentials, opt-ins, or cost rates in the environment."""
    return patch.dict(os.environ, {key: value for key, value in os.environ.items() if key not in PROVIDER_ENV}, clear=True)


def write_voice(text, path):
    Path(path).write_bytes(b"RIFF" + b"\0" * 2000)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "corpus" / "briefings.sqlite3", self.root / "corpus" / "audio")
        self.store.initialize()
        self.articles = read_articles(DEMO / "articles.jsonl")
        env = no_provider_env()
        env.start()
        self.addCleanup(env.stop)
        self.profile = load_profiles()[0].model_dump()

    def prepare(self):
        return prepare_corpus(self.store, self.articles)


class FixtureWithoutCredentialsTest(StoreTestCase):
    def test_fixture_briefing_and_costs_need_no_provider(self):
        stories = self.prepare()
        self.assertTrue(stories)
        with patch("backend.app.audio.offline_voice", side_effect=write_voice):
            briefing = generate_briefing(self.store, self.profile)
        self.assertEqual("ready", briefing["audio_status"])
        self.assertTrue(briefing["segments"])
        report = usage_report(self.store, mode="demo")
        self.assertEqual(0, report["preparation_usd"])
        self.assertEqual(0, report["per_briefing_usd"])
        self.assertEqual(0, report["ten_x_preparation_usd"])
        self.assertTrue(all(row["estimated_usd"] == 0 for row in report["stages"]))

    def test_demo_costs_are_zero_even_without_a_system_voice(self):
        self.prepare()
        with patch("backend.app.audio.offline_voice", side_effect=RuntimeError("no voice")):
            generate_briefing(self.store, self.profile)
        report = usage_report(self.store, mode="demo")
        self.assertEqual((0, 0), (report["preparation_usd"], report["per_briefing_usd"]))

    def test_empty_corpus_explains_how_to_import(self):
        with self.assertRaisesRegex(ValueError, "Run the import command"):
            generate_briefing(self.store, self.profile)


class AudioRecoveryTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.prepare()
        patcher = patch.object(main, "store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_synthesis_failure_keeps_transcript_and_records_no_speech(self):
        with patch("backend.app.audio.offline_voice", side_effect=subprocess.TimeoutExpired("voice", 60)):
            briefing = generate_briefing(self.store, self.profile)
        saved = self.store.briefing(briefing["id"])
        self.assertTrue(saved["segments"])
        self.assertEqual("unavailable", saved["audio_status"])
        self.assertIsNone(saved["audio_url"])
        self.assertEqual([], list(self.store.audio_dir.iterdir()))
        self.assertNotIn("speech_generation", {row["stage"] for row in self.store.usage("demo")})

    def test_retry_after_failure_then_idempotent(self):
        with patch("backend.app.audio.offline_voice", side_effect=RuntimeError("no voice")):
            briefing = generate_briefing(self.store, self.profile)
        with patch("backend.app.audio.offline_voice", side_effect=write_voice) as voice:
            first = main.retry_audio(briefing["id"])
            second = main.retry_audio(briefing["id"])
        self.assertEqual("ready", first["audio_status"])
        self.assertEqual(first, second)
        self.assertEqual(1, voice.call_count)
        self.assertEqual(first["segments"], briefing["segments"])
        self.assertTrue(main.audio(first["audio_url"].rsplit("/", 1)[-1]))

    def test_retry_rerenders_deleted_audio_instead_of_returning_dead_url(self):
        with patch("backend.app.audio.offline_voice", side_effect=write_voice) as voice:
            briefing = generate_briefing(self.store, self.profile)
            for file in self.store.audio_dir.iterdir():
                file.unlink()
            retried = main.retry_audio(briefing["id"])
        self.assertEqual(2, voice.call_count)
        self.assertEqual("ready", retried["audio_status"])
        self.assertTrue((self.store.audio_dir / retried["audio_url"].rsplit("/", 1)[-1]).is_file())

    def test_retry_unknown_briefing_is_404_and_releases_lock(self):
        with self.assertRaises(main.HTTPException) as caught:
            main.retry_audio("0" * 32)
        self.assertEqual(404, caught.exception.status_code)
        self.assertFalse(main.generation_lock.locked())

    def test_busy_retry_returns_unchanged_transcript(self):
        with patch("backend.app.audio.offline_voice", side_effect=RuntimeError("no voice")):
            briefing = generate_briefing(self.store, self.profile)
        main.generation_lock.acquire()
        try:
            with patch.object(main, "RETRY_WAIT_SECONDS", 0.01):
                result = main.retry_audio(briefing["id"])
        finally:
            main.generation_lock.release()
        self.assertEqual("unavailable", result["audio_status"])
        self.assertEqual(briefing["segments"], result["segments"])

    def test_status_save_failure_still_returns_transcript(self):
        original = self.store.save_briefing
        calls = []

        def flaky(briefing):
            calls.append(briefing["id"])
            if len(calls) > 1:
                raise RuntimeError("database is locked")
            original(briefing)

        with patch.object(self.store, "save_briefing", side_effect=flaky), \
                patch("backend.app.audio.offline_voice", side_effect=write_voice):
            briefing = generate_briefing(self.store, self.profile)
        self.assertTrue(briefing["segments"])
        self.assertEqual("pending", self.store.briefing(briefing["id"])["audio_status"])

    def test_live_provider_failures_fall_back_to_verified_transcript(self):
        # No credentials or opt-in: script generation and speech both fail, the transcript survives.
        with patch.object(service, "MODE", "live"):
            briefing = generate_briefing(self.store, self.profile)
        self.assertTrue(briefing["segments"])
        self.assertEqual("unavailable", briefing["audio_status"])
        self.assertEqual(briefing, self.store.briefing(briefing["id"]))


class CostSeparationTest(StoreTestCase):
    RATES = {
        "OPENAI_INPUT_USD_PER_MILLION_TOKENS": "1",
        "OPENAI_OUTPUT_USD_PER_MILLION_TOKENS": "2",
        "ELEVENLABS_USD_PER_1000_CHARS": "0.3",
    }

    def prepare_with_usage(self, tokens):
        real_prepare = service.prepare

        def recording_prepare(articles, providers):
            providers.record_usage("story_extraction", mode="live", input_tokens=tokens)
            return real_prepare(articles, providers)

        with patch.object(service, "prepare", side_effect=recording_prepare):
            return self.prepare()

    def save_briefings(self, count):
        for n in range(count):
            self.store.save_briefing({"id": f"{n:032x}", "segments": []})

    def test_reimport_replaces_preparation_usage(self):
        with patch.dict(os.environ, self.RATES):
            self.prepare_with_usage(2_000_000)
            self.prepare_with_usage(2_000_000)
            report = usage_report(self.store, mode="live")
        self.assertAlmostEqual(2.0, report["preparation_usd"])
        self.assertAlmostEqual(20.0, report["ten_x_preparation_usd"])

    def test_failed_import_keeps_previous_preparation_usage(self):
        self.prepare_with_usage(1_000_000)
        with self.assertRaises(ValueError):
            prepare_corpus(self.store, self.articles + self.articles[:1])
        self.assertEqual(1, {row["stage"]: row for row in self.store.usage("live")}["story_extraction"]["calls"])

    def test_briefing_usage_is_separate_from_preparation(self):
        with patch.dict(os.environ, self.RATES):
            self.prepare_with_usage(1_000_000)
            before = usage_report(self.store, mode="live")["preparation_usd"]
            self.save_briefings(2)
            self.store.record_usage("script_generation", mode="live", input_tokens=1_000_000, output_tokens=500_000)
            self.store.record_usage("speech_generation", mode="live", characters=1000)
            report = usage_report(self.store, mode="live")
        self.assertAlmostEqual(before, report["preparation_usd"])
        # Script: $2 over 2 briefings; speech: $0.30 over 1 successful render.
        self.assertAlmostEqual(1.3, report["per_briefing_usd"])

    def test_failed_audio_does_not_dilute_per_briefing_cost(self):
        with patch.dict(os.environ, self.RATES):
            self.save_briefings(1)
            self.store.record_usage("script_generation", mode="live", input_tokens=1_000_000)
            self.store.record_usage("speech_generation", mode="live", characters=1000)
            one = usage_report(self.store, mode="live")["per_briefing_usd"]
            self.save_briefings(2)  # second briefing: same script cost, audio failed
            self.store.record_usage("script_generation", mode="live", input_tokens=1_000_000)
            two = usage_report(self.store, mode="live")["per_briefing_usd"]
        self.assertAlmostEqual(one, two)

    def test_rates_set_after_calls_still_price_usage(self):
        self.store.record_usage("story_extraction", mode="live", input_tokens=1_000_000, estimated_usd=None)
        self.assertIsNone(usage_report(self.store, mode="live")["preparation_usd"])
        with patch.dict(os.environ, self.RATES):
            self.assertAlmostEqual(1.0, usage_report(self.store, mode="live")["preparation_usd"])

    def test_report_keeps_response_shape(self):
        report = usage_report(self.store, mode="live")
        self.assertEqual(
            {"mode", "article_count", "story_count", "stages", "preparation_usd", "per_briefing_usd", "ten_x_preparation_usd"},
            set(report),
        )
        self.assertEqual(
            ["ingestion", "story_extraction", "script_generation", "speech_generation"],
            [row["stage"] for row in report["stages"]],
        )


class ImportTest(StoreTestCase):
    def fixture_lines(self):
        return [line for line in (DEMO / "articles.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]

    def test_zip_with_nested_uppercase_and_mac_metadata(self):
        lines = self.fixture_lines()
        half = len(lines) // 2
        archive = self.root / "event.zip"
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("feeds/a.jsonl", "\n".join(lines[:half]))
            zipped.writestr("feeds/nested/B.JSONL", "\n".join(lines[half:]))
            zipped.writestr("__MACOSX/feeds/._a.jsonl", "\0binary")
            zipped.writestr("feeds/._b.jsonl", "\0binary")
            zipped.writestr("README.txt", "not a feed")
        self.assertEqual(len(lines), len(read_articles(archive)))

    def test_nested_directory_import(self):
        lines = self.fixture_lines()
        (self.root / "feeds" / "deep").mkdir(parents=True)
        (self.root / "feeds" / "a.jsonl").write_text("\n".join(lines[:3]), encoding="utf-8")
        (self.root / "feeds" / "deep" / "b.jsonl").write_text("\n".join(lines[3:]), encoding="utf-8")
        self.assertEqual(len(lines), len(read_articles(self.root / "feeds")))

    def test_missing_path_and_bad_zip_are_clear_errors(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            read_articles(self.root / "missing.zip")
        bad = self.root / "bad.zip"
        bad.write_text("not a zip")
        with self.assertRaisesRegex(ValueError, "ZIP"):
            read_articles(bad)

    def test_invalid_row_names_file_line_and_field(self):
        feed = self.root / "feed.jsonl"
        lines = self.fixture_lines()
        broken = json.loads(lines[1])
        broken["url"] = "not a url"
        feed.write_text("\n".join([lines[0], json.dumps(broken)]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"feed\.jsonl:2 \(check url\)"):
            read_articles(feed)

    def test_duplicate_ids_are_named(self):
        with self.assertRaisesRegex(ValueError, self.articles[0]["article_id"]):
            prepare_corpus(self.store, self.articles + self.articles[:1])

    def test_reimport_is_repeatable(self):
        first = self.prepare()
        prepare_corpus(self.store, self.articles[:3])
        again = self.prepare()
        self.assertEqual([story["id"] for story in first], [story["id"] for story in again])
        self.assertEqual(len(self.articles), len(self.store.articles()))

    def test_failed_live_extraction_keeps_previous_corpus(self):
        self.prepare()
        before = self.store.stories()
        live = Providers("live", self.store.record_usage)  # no opt-in, so extraction raises
        with patch.object(service, "get_providers", return_value=live):
            with self.assertRaisesRegex(ValueError, "previous corpus was kept"):
                prepare_corpus(self.store, self.articles[:3], allow_fallback=False)
        self.assertEqual(before, self.store.stories())

    def test_failed_live_extraction_can_opt_into_fallback(self):
        live = Providers("live", self.store.record_usage)
        with patch.object(service, "get_providers", return_value=live):
            self.assertTrue(prepare_corpus(self.store, self.articles, allow_fallback=True))
        self.assertTrue(live.extraction_failed)


class CleanupTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "data"

    def cli(self, *args, corpus="fixture"):
        env = {key: value for key, value in os.environ.items() if key not in PROVIDER_ENV}
        env.update(LIQUID_DATA_DIR=str(self.data), LIQUID_CORPUS=corpus, LIQUID_MODE="demo")
        return subprocess.run(
            [sys.executable, "-m", "backend.app", *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=120,
        )

    def test_store_purge_removes_files_and_is_repeatable(self):
        store = Store(self.data / "event" / "demo" / "briefings.sqlite3", self.data / "event" / "demo" / "audio")
        store.initialize()
        (store.audio_dir / ("a" * 32 + ".wav")).write_bytes(b"x")
        store.purge()
        store.purge()
        self.assertFalse((self.data / "event" / "demo").exists())

    def test_cli_import_reset_and_purge_are_repeatable(self):
        for _ in range(2):
            result = self.cli("import")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(14, json.loads(result.stdout)["articles"])
        self.assertEqual(0, self.cli("reset-demo", "--confirm").returncode)
        self.assertFalse((self.data / "fixture").exists())
        self.assertEqual(0, self.cli("reset-demo", "--confirm").returncode)

        event = self.data / "event-feed.jsonl"
        event.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEMO / "articles.jsonl", event)
        self.assertEqual(0, self.cli("import", str(event), corpus="event").returncode)
        self.assertTrue((self.data / "event" / "demo" / "briefings.sqlite3").is_file())
        refused = self.cli("purge-event", "--confirm")
        self.assertEqual(2, refused.returncode)
        self.assertTrue((self.data / "event").exists())
        for _ in range(2):
            self.assertEqual(0, self.cli("purge-event", "--confirm", corpus="event").returncode)
        self.assertFalse((self.data / "event").exists())

    def test_cli_reports_bad_paths_without_traceback(self):
        result = self.cli("import", str(self.data / "missing.zip"), corpus="event")
        self.assertEqual(2, result.returncode)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "git is not available")
class GitIgnoreTest(unittest.TestCase):
    def test_event_data_and_secrets_are_ignored_but_fixture_is_tracked(self):
        ignored = [
            "event.zip", "feed.jsonl", "liquid-briefing-data/feed.jsonl", "__MACOSX/x", ".env", ".env.local",
            "profiles.local.json", ".liquid-data/event/demo/briefings.sqlite3", "tmp/x.jsonl", "audio.mp3",
        ]
        tracked = ["backend/demo/articles.jsonl", "backend/demo/profiles.json", ".env.example"]
        for path in ignored + tracked:
            result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, capture_output=True)
            with self.subTest(path=path):
                self.assertEqual(0 if path in ignored else 1, result.returncode)


if __name__ == "__main__":
    unittest.main()
