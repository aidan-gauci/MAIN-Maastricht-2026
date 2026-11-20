import unittest

from backend.app.pipeline import introduce_once, spoken_text

INTRO = "Here are the reported measures."


def segment(story, text):
    return {"id": f"segment-{story}", "text": text, "fact_ids": [], "sources": [], "conflict": False}


class SpokenFramingTest(unittest.TestCase):
    def test_introduction_is_said_once_at_the_start(self):
        segments = introduce_once([segment("a", f"{INTRO} Policy rate, 3.25 percent."),
                                   segment("b", f"{INTRO} Saving rate, 7.1 percent.")])
        self.assertEqual([s["text"] for s in segments], [f"{INTRO} Policy rate, 3.25 percent.", "Saving rate, 7.1 percent."])

    def test_each_story_is_announced_by_its_headline_after_any_introduction(self):
        first = spoken_text(segment("a", f"{INTRO} Policy rate, 3.25 percent."), "Central bank holds rate", "en")
        later = spoken_text(segment("b", "Saving rate, 7.1 percent."), "Is Huang coming back?", "en")
        self.assertEqual(first, f"{INTRO} Central bank holds rate. Policy rate, 3.25 percent.")
        self.assertEqual(later, "Is Huang coming back? Saving rate, 7.1 percent.")

    def test_no_headline_or_non_english_output_keeps_the_text(self):
        text = "Leitzins, 3,25 Prozent."
        self.assertEqual(spoken_text(segment("a", text), "Central bank holds rate", "de"), text)
        self.assertEqual(spoken_text(segment("a", text), "", "en"), text)


if __name__ == "__main__":
    unittest.main()
