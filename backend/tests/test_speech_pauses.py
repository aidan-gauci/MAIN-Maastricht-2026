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

    def test_expressiveness_levels_map_to_distinct_voice_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            levels = {level: voice_settings(level) for level in ("calm", "balanced", "expressive")}
        self.assertEqual(levels["balanced"], {})
        self.assertGreater(levels["calm"]["stability"], levels["expressive"]["stability"])
        self.assertLess(levels["calm"]["style"], levels["expressive"]["style"])
        self.assertEqual(Providers("live", expressiveness="loud").expressiveness, "balanced")

    def test_briefing_request_stores_the_level_and_rejects_unknown_ones(self):
        import tempfile, uuid
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {"LIQUID_DATA_DIR": tempfile.mkdtemp()}), \
                patch("backend.app.audio.offline_voice", side_effect=lambda text, path: None):
            from backend.app.main import app
            with TestClient(app) as client:
                body = {"listener_id": str(uuid.uuid4()), "profile_id": "everyday"}
                made = client.post("/api/briefings", json={**body, "expressiveness": "expressive"})
                default = client.post("/api/briefings", json={"listener_id": str(uuid.uuid4()), "profile_id": "markets"})
                bad = client.post("/api/briefings", json={**body, "expressiveness": "loud"})
        self.assertEqual(made.json()["expressiveness"], "expressive")
        self.assertEqual(default.json()["expressiveness"], "balanced")
        self.assertEqual(bad.status_code, 422)

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
