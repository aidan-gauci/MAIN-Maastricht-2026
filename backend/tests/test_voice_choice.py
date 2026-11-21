import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app import config
from backend.app.audio import create_audio
from backend.app.providers import Providers


class VoiceCatalogueTest(unittest.TestCase):
    def test_live_mode_offers_curated_voices_and_demo_mode_only_the_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIEFING_VOICE_PRESETS", None)
            with patch.object(config, "MODE", "live"):
                live = [row["id"] for row in config.configured_voice_presets()]
            with patch.object(config, "MODE", "demo"):
                demo = [row["id"] for row in config.configured_voice_presets()]
        self.assertEqual(live[0], "default")
        self.assertIn("daniel", live)
        self.assertEqual(len(config.VOICE_CATALOGUE), 5)  # two male and three female voices
        self.assertEqual(len(live), 1 + len(config.VOICE_CATALOGUE))
        self.assertEqual(demo, ["default"])

    def test_presets_resolve_to_their_voice_and_unknown_ones_to_the_default(self):
        with patch.dict(os.environ, {"ELEVENLABS_VOICE_ID": "server-default"}), patch.object(config, "MODE", "live"):
            os.environ.pop("BRIEFING_VOICE_PRESETS", None)
            self.assertEqual(config.resolve_voice_id("daniel"), "onwK4e9ZLuTAKqWW03F9")
            self.assertEqual(config.resolve_voice_id("default"), "server-default")
            self.assertEqual(config.resolve_voice_id("nobody"), "server-default")
            self.assertEqual(Providers("live", voice_preset="lily").voice_id(), "pFZP5JQG7iQjIQuC4Bku")


class VoiceLanguageTest(unittest.TestCase):
    def test_each_voice_reports_its_verified_languages_and_unknown_voices_none(self):
        with patch.dict(os.environ, {"ELEVENLABS_VOICE_ID": "7jynqzOvnVP89nA7TB0m"}), patch.object(config, "MODE", "live"):
            os.environ.pop("BRIEFING_VOICE_PRESETS", None)
            self.assertIn("nl", config.verified_languages("brian"))
            self.assertNotIn("nl", config.verified_languages("daniel"))
            self.assertIn("nl", config.verified_languages("default"))  # Nick
            for row in config.VOICE_CATALOGUE:
                self.assertIn("en", config.verified_languages(row["id"]))
                self.assertIn("de", config.verified_languages(row["id"]))
        with patch.dict(os.environ, {"ELEVENLABS_VOICE_ID": "someone-else"}), patch.object(config, "MODE", "live"):
            self.assertIsNone(config.verified_languages("default"))


class VoiceCacheTest(unittest.TestCase):
    def test_the_same_text_in_two_voices_renders_two_files(self):
        store = SimpleNamespace(audio_dir=Path(tempfile.mkdtemp()), record_usage=lambda *a, **k: None)
        briefing = {"language": "en", "stories": [], "segments": [
            {"id": "segment-a", "text": "Policy rate, 3.25 percent.", "fact_ids": ["f1"]},
            {"id": "segment-b", "text": "Inflation, 2.6 percent.", "fact_ids": ["f2"]}]}

        def fake_synthesize(text, output, language):
            Path(output).write_bytes(b"ID3" + b"\0" * 2000)

        urls = []
        with patch.object(config, "MODE", "live"), patch.dict(os.environ, {"ELEVENLABS_VOICE_ID": "server-default"}), \
                patch("backend.app.audio._assemble_audio", side_effect=lambda clips, output, ffmpeg: Path(output).write_bytes(b"ID3" + b"\0" * 4000)), \
                patch("backend.app.audio.shutil.which", return_value="ffmpeg"):
            os.environ.pop("BRIEFING_VOICE_PRESETS", None)
            for preset in ("daniel", "lily", "daniel"):
                providers = Providers("live", voice_preset=preset)
                providers.synthesize = Mock(side_effect=fake_synthesize)
                urls.append(create_audio(briefing, store, providers, "live"))
        self.assertNotEqual(urls[0], urls[1])
        self.assertEqual(urls[0], urls[2])


class VoicePreviewTest(unittest.TestCase):
    def test_preview_is_fetched_once_then_served_from_cache_and_hidden_in_demo_mode(self):
        from fastapi.testclient import TestClient
        from backend.app import main
        fake_client = SimpleNamespace(voices=SimpleNamespace(get=Mock(return_value=SimpleNamespace(
            preview_url="https://storage.example.invalid/sample.mp3"))))
        sample = b"ID3" + b"\1" * 5000
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "store", SimpleNamespace(audio_dir=Path(directory))), \
                patch.dict("sys.modules", elevenlabs=SimpleNamespace(ElevenLabs=Mock(return_value=fake_client))), \
                patch.object(main, "urlopen", side_effect=lambda *a, **k: io.BytesIO(sample)) as fetch, \
                patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}):
            os.environ.pop("BRIEFING_VOICE_PRESETS", None)
            client = TestClient(main.app)
            with patch.object(main, "MODE", "live"), patch.object(config, "MODE", "live"):
                listed = client.get("/api/voice-presets").json()
                first = client.get("/api/voice-presets/daniel/preview")
                second = client.get("/api/voice-presets/daniel/preview")
                unknown = client.get("/api/voice-presets/nobody/preview")
            with patch.object(main, "MODE", "demo"), patch.object(config, "MODE", "demo"):
                demo = client.get("/api/voice-presets/daniel/preview")
        self.assertTrue(all(row["preview"] for row in listed))
        self.assertEqual((first.status_code, first.content), (200, sample))
        self.assertEqual(second.content, sample)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(demo.status_code, 404)


if __name__ == "__main__":
    unittest.main()
