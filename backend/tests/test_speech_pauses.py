import os
import unittest
from unittest.mock import patch

from backend.app.providers import FIGURE_PAUSE, INTRO_PAUSE, Providers, voice_settings, with_pauses


class SpeechPauseTest(unittest.TestCase):
    def test_voice_settings_come_from_the_environment_and_speed_is_clamped(self):
        with patch.dict(os.environ, {"ELEVENLABS_SPEED": "1.5", "ELEVENLABS_STABILITY": "0.35", "ELEVENLABS_STYLE": "bad"}):
            self.assertEqual(voice_settings(), {"speed": 1.2, "stability": 0.35})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(voice_settings(), {})

    def test_listeners_cannot_set_an_expressiveness_level(self):
        # Style levels multiply rendered audio for little audible difference, so they are not offered:
        # the request field is rejected and no level is stored on the briefing.
        import tempfile, uuid
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {"BRIEFING_DATA_DIR": tempfile.mkdtemp()}), \
                patch("backend.app.audio.offline_voice", side_effect=lambda *args, **kwargs: None):
            from backend.app.main import app
            with TestClient(app) as client:
                body = {"listener_id": str(uuid.uuid4()), "profile_id": "everyday"}
                made = client.post("/api/briefings", json=body)
                rejected = client.post("/api/briefings", json={**body, "expressiveness": "expressive"})
        self.assertEqual(made.status_code, 201, made.text)
        self.assertNotIn("expressiveness", made.json())
        self.assertEqual(rejected.status_code, 422)
        self.assertFalse(hasattr(Providers("live"), "expressiveness"))

    def test_pauses_follow_the_intro_and_each_figure_without_splitting_decimals(self):
        text = "In plain terms, here’s what the sources report. Policy rate, 3.25 percent. Inflation, 2.6 percent."
        self.assertEqual(
            with_pauses(text),
            f"In plain terms, here’s what the sources report. {INTRO_PAUSE} Policy rate, 3.25 percent. "
            f"{FIGURE_PAUSE} Inflation, 2.6 percent.",
        )

    def test_single_sentence_is_unchanged(self):
        self.assertEqual(with_pauses("Policy rate, 3.25 percent."), "Policy rate, 3.25 percent.")


if __name__ == "__main__":
    unittest.main()
