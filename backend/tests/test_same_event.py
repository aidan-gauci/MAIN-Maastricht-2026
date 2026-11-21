import unittest

from backend.app import pipeline
from backend.app.clustering import merge_same_event

FIGURES = "Shares fell 29% to $23.10; the suit covers $4.5 billion of buybacks and 118 million shares."
OTHER = "The board met 3 times; 12 directors voted and 7 abstained over 45 minutes."


def article(article_id, source, published, body=FIGURES, language="en"):
    return {"article_id": article_id, "source": source, "language": language, "title": article_id, "body": body,
            "publication_date": published}


class SameEventMergeTest(unittest.TestCase):
    def test_two_outlets_reporting_one_event_merge(self):
        groups = [[article("a", "Invezz", "2026-11-16T08:00:00Z")], [article("b", "IBTimes", "2026-11-16T14:00:00Z")]]
        self.assertEqual(len(merge_same_event(groups, [[1.0, 0.0], [0.95, 0.05]])), 1)

    def test_same_topic_on_another_day_same_outlet_or_different_figures_stay_apart(self):
        vectors = [[1.0, 0.0], [0.95, 0.05]]
        for second in (
            article("b", "IBTimes", "2026-11-18T08:00:00Z"),            # two days later: an ongoing saga
            article("b", "Invezz - German", "2026-11-16T09:00:00Z"),    # same outlet's edition
            article("b", "IBTimes", "2026-11-16T09:00:00Z", OTHER),     # different figures
        ):
            with self.subTest(second=second["source"]):
                groups = [[article("a", "Invezz", "2026-11-16T08:00:00Z")], [second]]
                self.assertEqual(len(merge_same_event(groups, vectors)), 2)

    def test_dissimilar_embeddings_stay_apart_and_bad_vectors_are_ignored(self):
        groups = [[article("a", "Invezz", "2026-11-16T08:00:00Z")], [article("b", "IBTimes", "2026-11-16T09:00:00Z")]]
        self.assertEqual(len(merge_same_event(groups, [[1.0, 0.0], [0.0, 1.0]])), 2)
        self.assertEqual(len(merge_same_event(groups, [[1.0, 0.0]])), 2)


class DiversityTest(unittest.TestCase):
    def test_near_twin_stories_are_passed_over_but_unembedded_ones_are_kept(self):
        stories = [{"id": "a", "embedding": [1.0, 0.0]}, {"id": "b", "embedding": [0.97, 0.1]},
                   {"id": "c", "embedding": [0.0, 1.0]}, {"id": "d"}]
        self.assertEqual([s["id"] for s in pipeline.diversify(stories)], ["a", "c", "d"])
        # A briefing needs only a handful: stop comparing once enough are kept.
        self.assertEqual([s["id"] for s in pipeline.diversify(stories, limit=2)], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
