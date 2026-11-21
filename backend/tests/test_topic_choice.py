"""The topics offered to a wildcard listener: every topic of a small corpus, a short useful list of a large one."""
import unittest

from backend.app import service


def story(n, topics, title="Quarterly report", evidence="Output rose 3 percent in the quarter."):
    article = {"article_id": f"a{n}", "title": title, "source": "Example Wire", "language": "en",
               "url": f"https://example.invalid/a{n}"}
    fact = {"id": f"f{n}", "article_id": f"a{n}", "evidence": evidence, "statement": evidence, "value": "3",
            "unit": "percent", "language": "en", "conflict_status": "supported"}
    return {"id": f"s{n}", "title": title, "topics": topics, "publication_date": "2026-11-19", "articles": [article],
            "facts": [fact], "conflicts": []}


class TopicChoiceTest(unittest.TestCase):
    def test_a_small_corpus_offers_every_topic_in_order(self):
        stories = [story(1, ["rates", "housing"]), story(2, ["energy"])]
        self.assertEqual(service.list_topics(stories), ["energy", "housing", "rates"])

    def test_a_large_corpus_offers_subject_areas_then_common_subjects_only(self):
        stories = [story(n, [f"one-off subject {n}"]) for n in range(60)]
        stories += [story(100 + n, ["shipbuilding"], evidence="Housing starts rose 3 percent in the month.") for n in range(6)]
        stories += [story(200 + n, ["Shipbuilding", "aktien"]) for n in range(3)]
        picks = [story(300 + n, ["stock picks"], title="3 Chip Stocks to Own Today") for n in range(9)]
        stories += picks
        topics = service.list_topics(stories)
        self.assertLessEqual(len(topics), service.MAX_LISTED_TOPICS)
        self.assertEqual(topics[0], "housing")                 # a subject area several stories report on
        self.assertIn("shipbuilding", topics)                  # tagged on several stories, listed once
        self.assertNotIn("Shipbuilding", topics)
        self.assertFalse([topic for topic in topics if topic.startswith("one-off")])
        self.assertNotIn("stock picks", topics)                # only stories that can be spoken count
        self.assertIn("aktien", topics)                        # another language's word stands in when alone...
        stories += [story(400 + n, ["stocks"]) for n in range(5)]
        topics = service.list_topics(stories)
        self.assertIn("stocks", topics)
        self.assertNotIn("aktien", topics)                     # ...and gives way to the same subject in English


if __name__ == "__main__":
    unittest.main()
