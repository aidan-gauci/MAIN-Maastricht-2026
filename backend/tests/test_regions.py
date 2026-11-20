import unittest

from backend.app.pipeline import corpus_regions


class CorpusRegionsTest(unittest.TestCase):
    def test_only_regions_the_stories_carry_are_offered(self):
        stories = [{"articles": [{"country_code": "NL"}]}, {"articles": [{"country": "us"}]}, {"articles": [{}]}]
        self.assertEqual(corpus_regions(stories), ["all", "Europe", "NL", "US"])

    def test_corpus_without_geography_offers_all_only(self):
        self.assertEqual(corpus_regions([{"articles": [{"region": None, "country": None}]}]), ["all"])


if __name__ == "__main__":
    unittest.main()
