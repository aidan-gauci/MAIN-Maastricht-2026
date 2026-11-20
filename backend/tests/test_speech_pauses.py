import os
import unittest
from unittest.mock import patch

from backend.app.providers import FIGURE_PAUSE, INTRO_PAUSE, voice_settings, with_pauses


class SpeechPauseTest(unittest.TestCase):
    def test_voice_settings_come_from_the_environment_and_speed_is_clamped(self):
        with patch.dict(os.environ, {"ELEVENLABS_SPEED": "1.5", "ELEVENLABS_STABILITY": "0.35", "ELEVENLABS_STYLE": "bad"}):
            self.assertEqual(voice_settings(), {"speed": 1.2, "stability": 0.35})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(voice_settings(), {})

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
