import unittest

from backend.app.providers import FIGURE_PAUSE, INTRO_PAUSE, with_pauses


class SpeechPauseTest(unittest.TestCase):
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
