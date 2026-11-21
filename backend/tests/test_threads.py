"""Event threads: developments of one event are marked together, never merged. Synthetic stories only."""
import unittest

from backend.app import pipeline
from backend.app.threads import assign_threads


def story(identifier, title, day, vector=None, language="en"):
    row = {"id": identifier, "title": title, "publication_date": f"2026-11-{day:02d}",
           "articles": [{"article_id": identifier, "title": title, "language": language}]}
    if vector:
        row["embedding"] = vector
    return row


def corpus(*stories, fillers=100):
    """Stories under test among unrelated ones, so that "rare" and "common" names mean something."""
    quiet = [story(f"filler-{i}", f"Quiet day for sector number {i}", 1 + i % 28) for i in range(fillers)]
    return list(stories) + quiet


def threads_of(stories):
    return {s["id"]: s.get("thread_id") for s in stories if not s["id"].startswith("filler")}


class EventThreadTest(unittest.TestCase):
    SAGA = [
        ("s1", "Zorbo steps down as Acme chief", 13, [1.0, 0.0]),
        ("s2", "Zorbo allies plan a return to Acme", 14, [0.9, 0.1]),
        ("s3", "Acme board weighs a Zorbo comeback", 16, [0.95, 0.05]),
    ]

    def test_developments_of_one_event_share_a_thread(self):
        stories = corpus(*(story(*row) for row in self.SAGA))
        threads = assign_threads(stories)
        ids = threads_of(stories)
        self.assertEqual(1, len(threads))
        self.assertEqual(1, len(set(ids.values())))
        self.assertTrue(all(value.startswith("thread-") for value in ids.values()))
        # Stories keep their own identity and facts; nothing is merged.
        self.assertEqual(["s1", "s2", "s3"], [s["id"] for s in threads[0]])

    def test_the_most_connected_story_names_the_thread(self):
        stories = corpus(story("s1", "Zorbo steps down as Acme chief", 13, [1.0, 0.0]),
                         story("s2", "Zorbo allies plan a return to Acme", 14, [0.7, 0.7]),
                         story("s3", "Acme board weighs a Zorbo comeback", 15, [0.0, 1.0]))
        assign_threads(stories)
        self.assertEqual({"Zorbo allies plan a return to Acme"}, {s["thread_title"] for s in stories[:3]})

    def test_ids_are_stable_and_unthreaded_stories_carry_no_marker(self):
        first, second = corpus(*(story(*row) for row in self.SAGA)), corpus(*(story(*row) for row in self.SAGA))
        assign_threads(first)
        assign_threads(second)
        self.assertEqual(threads_of(first), threads_of(second))
        self.assertTrue(all("thread_id" not in s and "thread_title" not in s for s in first[3:]))

    def test_a_name_common_to_many_stories_does_not_make_a_thread(self):
        busy = [story(f"q{i}", f"Quux shares move on report {i}", 13 + i % 3, [1.0, 0.0]) for i in range(15)]
        stories = corpus(*busy)
        assign_threads(stories)
        self.assertEqual({None}, set(threads_of(stories).values()))

    def test_stories_about_different_things_stay_apart_even_when_they_share_a_name(self):
        stories = corpus(story("a", "Zorbo steps down as Acme chief", 13, [1.0, 0.0]),
                         story("b", "Zorbo opens a bakery near Acme", 14, [0.0, 1.0]))
        assign_threads(stories)
        self.assertEqual({None}, set(threads_of(stories).values()))

    def test_a_later_report_outside_the_window_starts_no_thread(self):
        stories = corpus(story("a", "Zorbo steps down as Acme chief", 5, [1.0, 0.0]),
                         story("b", "Zorbo returns to Acme after a week", 19, [1.0, 0.0]))
        assign_threads(stories)
        self.assertEqual({None}, set(threads_of(stories).values()))

    def test_digests_do_not_join_threads(self):
        stories = corpus(*(story(*row) for row in self.SAGA[:2]),
                         story("d", "Evening digest: Zorbo returns, Acme rallies", 14, [1.0, 0.0]))
        assign_threads(stories)
        ids = threads_of(stories)
        self.assertIsNone(ids["d"])
        self.assertIsNotNone(ids["s1"])
        self.assertEqual(ids["s1"], ids["s2"])

    def test_a_name_spread_over_months_is_an_ongoing_subject_not_an_event(self):
        stories = corpus(story("a", "Zorbo steps down as Acme chief", 3, [1.0, 0.0]),
                         story("b", "Zorbo returns to Acme", 4, [1.0, 0.0]))
        stories[0]["publication_date"], stories[1]["publication_date"] = "2026-08-03", "2026-11-04"
        assign_threads(stories)
        self.assertEqual({None}, set(threads_of(stories).values()))

    def test_a_story_naming_two_events_goes_with_one_and_does_not_join_them(self):
        same = [1.0, 0.0]
        stories = corpus(
            story("x1", "Zorbo steps down as Acme chief", 13, same), story("x2", "Zorbo allies plan a return", 14, same),
            story("y1", "Quux faces a shareholder lawsuit", 13, same), story("y2", "Quux denies the lawsuit claims", 14, same),
            story("bridge", "Zorbo exit hurts Quux in the lawsuit", 15, same),
        )
        threads = assign_threads(stories)
        ids = threads_of(stories)
        self.assertEqual(2, len(threads))
        self.assertEqual(ids["x1"], ids["x2"])
        self.assertEqual(ids["y1"], ids["y2"])
        self.assertNotEqual(ids["x1"], ids["y1"])
        self.assertIn(ids["bridge"], {ids["x1"], ids["y1"]})

    def test_only_english_headlines_name_a_thread(self):
        stories = corpus(story("a", "Zorbo tritt als Acme-Chef zurueck", 13, [1.0, 0.0], "de"),
                         story("b", "Zorbo kehrt zu Acme zurueck", 14, [1.0, 0.0], "de"))
        assign_threads(stories)
        self.assertEqual({None}, set(threads_of(stories).values()))

    def test_without_embeddings_it_takes_four_shared_headline_words(self):
        stories = corpus(story("a", "Zorbo steps down as Acme chief tonight", 13),
                         story("b", "Acme chief Zorbo steps down early", 14),
                         story("c", "Zorbo bakes bread", 14))
        assign_threads(stories)
        ids = threads_of(stories)
        self.assertEqual(ids["a"], ids["b"])
        self.assertIsNotNone(ids["a"])
        self.assertIsNone(ids["c"])

    def test_markers_of_an_earlier_assignment_are_cleared(self):
        stories = corpus(*(story(*row) for row in self.SAGA))
        assign_threads(stories)
        stories[2]["publication_date"] = "2026-11-28"
        assign_threads(stories)
        ids = threads_of(stories)
        self.assertIsNone(ids["s3"])
        self.assertNotIn("thread_title", stories[2])
        self.assertEqual(ids["s1"], ids["s2"])


class OneStoryPerThreadTest(unittest.TestCase):
    def test_a_briefing_plays_one_development_of_an_event(self):
        ranked = [{"id": "a", "thread_id": "t1"}, {"id": "b", "thread_id": "t1"}, {"id": "c"},
                  {"id": "d", "thread_id": "t2"}, {"id": "e", "thread_id": "t2"}, {"id": "f"}]
        self.assertEqual(["a", "c", "d", "f"], [s["id"] for s in pipeline.diversify(ranked)])

    def test_the_higher_ranked_story_of_a_thread_is_the_one_kept(self):
        self.assertEqual(["late"], [s["id"] for s in pipeline.diversify(
            [{"id": "late", "thread_id": "t"}, {"id": "early", "thread_id": "t"}])])


if __name__ == "__main__":
    unittest.main()
