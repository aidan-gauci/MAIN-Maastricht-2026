import unittest
from unittest.mock import Mock

from backend.app import pipeline
from backend.app.providers import Providers

BODY = ("Production fell short. factory production growth eased from 5.5% to 4.8% year on year, under the 5.3% consensus "
        "from analysts. Meanwhile, parcel shipments climbed 28.7% year on year in euro terms, despite weak demand. "
        "Final sales fell 0.5% qoq, marking a sharp drop.")
ARTICLE = {"article_id": "a1", "title": "Industrial output slows", "body": BODY, "source": "Test Wire",
           "url": "https://example.invalid/a1", "language": "en", "publication_date": "2026-11-19"}


def extract(*facts):
    live = Mock(spec=Providers, live=True)
    live.extract.return_value = {"stories": [{"title": ARTICLE["title"], "topics": ["industry"], "facts": [
        {"article_id": "a1", "value": value, "unit": unit, "evidence": evidence} for value, unit, evidence in facts]}]}
    stories = pipeline.prepare([dict(ARTICLE)], live)
    return [fact for story in stories for fact in story["facts"]]


class LiveEvidenceTest(unittest.TestCase):
    def test_reworded_quote_tails_and_capitals_still_cite_exact_article_text(self):
        facts = extract(
            ("4.8", "%", "Factory production growth eased from 5.5% to 4.8% year on year, short of consensus."),
            ("28.7", "%", "Parcel shipments climbed 28.7% year on year in euro terms."),
            ("-0.5", "%", "Final sales fell 0.5% qoq."),
        )
        self.assertEqual(sorted(f["value"] for f in facts), ["0.5", "28.7", "4.8"])
        for fact in facts:
            self.assertIn(fact["evidence"], BODY)

    def test_figures_absent_from_the_article_are_still_rejected(self):
        facts = extract(
            ("4.8", "%", "Factory production growth eased from 5.5% to 4.8% year on year."),
            ("4.9", "%", "Factory production growth eased from 5.5% to 4.9% year on year."),
            ("12", "%", "Profits rose 12% this year."),
            ("28.7", "billion", "Parcel shipments climbed 28.7% year on year in euro terms."),
        )
        self.assertEqual([(f["value"], f["unit"]) for f in facts], [("4.8", "percent")])


if __name__ == "__main__":
    unittest.main()
