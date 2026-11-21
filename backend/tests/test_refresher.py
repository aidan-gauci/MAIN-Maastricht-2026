"""Refreshers: a figure heard within the memory window is never replayed as news, but may be recalled in one short
sentence when a new figure for the same measure follows. Synthetic stories only."""
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.app import pipeline
from backend.app.pipeline import _NARRATION_FRAMES, heard_figures, introduce_once, narrate, rank, spoken_text
from backend.app.providers import Providers
from backend.app.service import NoRelevantStories, generate_briefing, iso_utc, utc_now
from backend.app.storage import Store
from backend.app.summaries import prepare_summaries

RATE_OLD = ("rate-old", "3.25", "The Freedonia central bank set the policy rate at 3.25%.")
RATE_NEW = ("rate-new", "3.0", "The Freedonia central bank cut the policy rate to 3.0%.")
INFLATION_OLD = ("inflation-old", "2.6", "Freedonia inflation stayed at 2.6%.")
INFLATION_NEW = ("inflation-new", "2.4", "Freedonia inflation eased to 2.4%.")
RECALL = {"en": "Last time, {} stood at {}.", "de": "Zuletzt: {}, {}.", "nl": "Vorige keer: {}, {}."}


def fact(article, spec, unit="%", **fields):
    key, value, evidence = spec
    return {"id": f"fact-{article}-{key}", "article_id": article, "statement": evidence, "evidence": evidence,
            "value": value, "unit": unit, "language": "en", "conflict_status": "supported", **fields}


def story(story_id, *facts, thread=None, day="2026-11-19", title="Freedonia bank update"):
    articles = {}
    for item in facts:
        articles.setdefault(item["article_id"], {
            "article_id": item["article_id"], "source": "Test Wire", "title": f"{title} ({item['article_id']})",
            "url": f"https://example.invalid/{item['article_id']}", "language": "en",
        })
    return {"id": story_id, "title": title, "topics": ["rates", "inflation"], "publication_date": day,
            "articles": list(articles.values()), "facts": list(facts), **({"thread_id": thread} if thread else {})}


def listener(language="en", **fields):
    return {"id": "listener", "language": language, "expertise": "intermediate", "duration": 5,
            "topic_weights": {"rates": 1.0}, **fields}


def refresh(corpus, target, spoken, language="en", providers=None, first_heard=None, **fields):
    """Narrate ``target`` the way a returning listener hears it: heard facts removed, then recalled."""
    profile = listener(language, **fields)
    ranked = rank(corpus, profile, spoken_fact_ids=spoken)
    ranked = [row for row in ranked if row["id"] == target["id"]]
    return narrate(ranked, profile, providers, heard_figures(corpus, spoken, first_heard))


def sentences(text):
    return pipeline._script_sentences(text)


class RefresherNarrationTest(unittest.TestCase):
    def setUp(self):
        self.old = fact("a1", RATE_OLD)
        self.new = fact("a2", RATE_NEW)

    def test_a_new_value_in_the_same_story_recalls_what_the_listener_heard_in_each_language(self):
        both = story("s1", self.old, self.new)
        expected = {
            "en": ("Last time, policy rate stood at 3.25 percent.", "Policy rate, 3.0 percent."),
            "de": ("Zuletzt: Leitzins, 3,25 Prozent.", "Leitzins, 3,0 Prozent."),
            "nl": ("Vorige keer: beleidsrente, 3,25 procent.", "Beleidsrente, 3,0 procent."),
        }
        for language, (recall, claim) in expected.items():
            with self.subTest(language=language):
                segment, = refresh([both], both, [self.old["id"]], language)
                self.assertEqual(segment["text"], f"{_NARRATION_FRAMES[language]['intermediate']} {recall} {claim}")
                # The recalled fact is cited with its own article, and marked as recalled.
                self.assertEqual(segment["fact_ids"], [self.new["id"], self.old["id"]])
                self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
                self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2", "a1"])

    def test_a_new_value_in_a_story_of_the_same_event_thread_recalls_it_with_its_own_source(self):
        earlier = story("s1", self.old, thread="thread-1", day="2026-11-17")
        later = story("s2", self.new, thread="thread-1")
        segment, = refresh([earlier, later], later, [self.old["id"]])
        self.assertEqual(sentences(segment["text"])[1:], ["Last time, policy rate stood at 3.25 percent.", "Policy rate, 3.0 percent."])
        self.assertEqual(segment["fact_ids"], [self.new["id"], self.old["id"]])
        self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
        self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2", "a1"])
        self.assertEqual(segment["sources"][1]["url"], "https://example.invalid/a1")

    def test_nothing_is_recalled_without_an_update_to_the_same_measure_in_the_same_story_or_thread(self):
        inflation = fact("a1", INFLATION_OLD)
        revenue_old = fact("a1", ("revenue-old", "4.2", "Freedonia revenue reached 4.2 billion euros."), unit="billion")
        revenue_new = fact("a2", ("revenue-new", "610", "Freedonia revenue reached 610 million euros."), unit="million")
        again = fact("a2", ("rate-again", "3.25", "The Freedonia central bank kept the policy rate at 3.25%."))
        cases = {
            "the same value again": (story("s1", self.old, thread="t"), story("s2", again, thread="t"), self.old),
            "a different measure": (story("s1", inflation, thread="t"), story("s2", self.new, thread="t"), inflation),
            "a unit that is not comparable": (story("s1", revenue_old, thread="t"), story("s2", revenue_new, thread="t"), revenue_old),
            "an unrelated story": (story("s1", self.old, thread="t"), story("s2", self.new, thread="other"), self.old),
            "a story with no thread": (story("s1", self.old), story("s2", self.new), self.old),
            "a thread on one side only": (story("s1", self.old, thread="t"), story("s2", self.new), self.old),
        }
        for label, (heard, current, spoken) in cases.items():
            with self.subTest(label):
                segment, = refresh([heard, current], current, [spoken["id"]])
                self.assertNotIn("refresher_fact_ids", segment)
                self.assertNotIn("Last time", segment["text"])
                self.assertEqual(segment["fact_ids"], [current["facts"][0]["id"]])
                self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2"])

    def test_a_fact_outside_the_memory_window_or_a_new_listener_has_nothing_to_recall(self):
        both = story("s1", self.old, self.new)
        # The caller passes only the ids heard within the window, so an expired one never arrives.
        self.assertEqual(heard_figures([both], []), [])
        self.assertEqual(heard_figures([both], None), [])
        segment, = refresh([both], both, [])
        self.assertNotIn("Last time", segment["text"])
        self.assertEqual(segment["fact_ids"], [self.old["id"], self.new["id"]])
        segment, = narrate([both], listener(), None)
        self.assertNotIn("refresher_fact_ids", segment)

    def test_a_heard_fact_that_is_no_longer_in_the_corpus_is_never_recalled(self):
        current = story("s2", self.new, thread="t")
        segment, = refresh([current], current, [self.old["id"]])
        self.assertNotIn("Last time", segment["text"])
        self.assertEqual(heard_figures([current], [self.old["id"]]), [])

    def test_a_conflicted_claim_gets_no_refresher(self):
        conflicted = fact("a2", RATE_NEW, conflict_status="conflict", conflict_group="conflict-1")
        both = story("s1", self.old, conflicted)
        segment, = refresh([both], both, [self.old["id"]])
        self.assertNotIn("Last time", segment["text"])

    def test_at_most_one_figure_is_recalled_per_segment_and_it_comes_right_before_its_new_figure(self):
        old = [self.old, fact("a1", INFLATION_OLD)]
        new = [self.new, fact("a2", INFLATION_NEW)]
        both = story("s1", *old, *new)
        spoken = [item["id"] for item in old]
        segment, = refresh([both], both, spoken)
        intro = _NARRATION_FRAMES["en"]["intermediate"]
        self.assertEqual(segment["text"], f"{intro} Last time, policy rate stood at 3.25 percent. "
                                          "Policy rate, 3.0 percent. Inflation, 2.4 percent.")
        self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
        self.assertEqual(segment["fact_ids"], [self.new["id"], new[1]["id"], self.old["id"]])
        # The new figure that is spoken first is the one given context, so the recalled sentence follows the listener's interests.
        segment, = refresh([both], both, spoken, topic_weights={"inflation": 1.0})
        self.assertEqual(segment["text"], f"{intro} Last time, inflation stood at 2.6 percent. "
                                          "Inflation, 2.4 percent. Policy rate, 3.0 percent.")
        self.assertEqual(segment["refresher_fact_ids"], [old[1]["id"]])
        self.assertEqual(segment["text"].count("Last time"), 1)

    def test_a_heard_figure_is_never_a_claim_again(self):
        both = story("s1", self.old, self.new)
        for language in ("en", "de", "nl"):
            with self.subTest(language=language):
                # Even handed a story that still holds the heard fact, only the recalled sentence says it.
                segment, = narrate([both], listener(language), None, heard_figures([both], [self.old["id"]]))
                recall = RECALL[language].split("{}")[0]
                spoken = sentences(segment["text"])
                self.assertEqual(sum("3,25" in s or "3.25" in s for s in spoken), 1)
                self.assertTrue(next(s for s in spoken if "3,25" in s or "3.25" in s).startswith(recall))
                self.assertEqual(segment["fact_ids"], [self.new["id"], self.old["id"]])

    def test_the_figure_heard_most_recently_is_the_one_recalled(self):
        oldest = fact("a0", ("rate-oldest", "3.5", "The Freedonia central bank set the policy rate at 3.5%."))
        corpus = [story("s0", oldest, thread="t", day="2026-11-15"), story("s1", self.old, thread="t", day="2026-11-17"),
                  story("s2", self.new, thread="t")]
        spoken = [oldest["id"], self.old["id"]]
        for heard_at, expected in (
            ({oldest["id"]: "2026-11-15T07:00:00Z", self.old["id"]: "2026-11-17T07:00:00Z"}, "3.25"),
            ({oldest["id"]: "2026-11-17T07:00:00Z", self.old["id"]: "2026-11-15T07:00:00Z"}, "3.5"),
            # Without history, the later-published story decides.
            (None, "3.25"),
        ):
            with self.subTest(heard_at=heard_at):
                segment, = narrate([corpus[2]], listener(), None, heard_figures(corpus, spoken, heard_at))
                self.assertIn(f"Last time, policy rate stood at {expected} percent.", segment["text"])
                self.assertEqual(sum("Last time" in s for s in sentences(segment["text"])), 1)

    def test_the_voice_and_the_briefing_keep_their_introduction_and_headline(self):
        both = story("s1", self.old, self.new)
        first, = refresh([both], both, [self.old["id"]])
        briefing = introduce_once([first, dict(first, id="segment-s9")])
        intro = _NARRATION_FRAMES["en"]["intermediate"]
        self.assertTrue(briefing[0]["text"].startswith(intro + " Last time"))
        self.assertTrue(briefing[1]["text"].startswith("Last time, policy rate"))
        self.assertEqual(spoken_text(briefing[1], "Bank cuts rates", "en"),
                         "Bank cuts rates. Last time, policy rate stood at 3.25 percent. Policy rate, 3.0 percent.")


class LiveRefresherTest(unittest.TestCase):
    def setUp(self):
        old = [fact("a1", RATE_OLD), fact("a1", INFLATION_OLD)]
        new = [fact("a2", RATE_NEW), fact("a2", INFLATION_NEW)]
        self.story = story("s1", *old, *new)
        self.spoken = [item["id"] for item in old]
        self.offline, = refresh([self.story], self.story, self.spoken)
        self.intro, self.recall, self.rate, self.inflation = sentences(self.offline["text"])
        self.live = Mock(spec=Providers, live=True)

    def narrate(self, draft):
        self.live.generate_script.return_value = draft
        segment, = refresh([self.story], self.story, self.spoken, providers=self.live)
        return segment

    def test_the_model_may_reorder_while_the_recalled_figure_stays_before_its_new_figure(self):
        reordered = f"{self.intro} {self.inflation} {self.recall} {self.rate}"
        segment = self.narrate(reordered)
        self.assertEqual(segment["text"], reordered)
        self.assertEqual(segment["refresher_fact_ids"], self.offline["refresher_fact_ids"])
        self.assertEqual(segment["fact_ids"], self.offline["fact_ids"])
        prompt = self.live.generate_script.call_args.args[0]
        self.assertIn(self.recall, prompt)
        self.assertIn("recalls an earlier figure", prompt)

    def test_a_draft_that_separates_the_recalled_figure_or_changes_it_falls_back_to_the_verified_text(self):
        for label, draft in (
            ("separated", f"{self.intro} {self.recall} {self.inflation} {self.rate}"),
            ("changed value", f"{self.intro} {self.recall.replace('3.25', '3.5')} {self.rate} {self.inflation}"),
            ("dropped", f"{self.intro} {self.rate} {self.inflation}"),
            ("invented", f"{self.intro} {self.recall} {self.rate} {self.inflation} Prices are about to rise."),
            ("introduction moved", f"{self.recall} {self.intro} {self.rate} {self.inflation}"),
        ):
            with self.subTest(label):
                self.assertEqual(self.narrate(draft), self.offline)

    def test_stories_without_a_recalled_figure_keep_the_original_prompt(self):
        plain = story("s2", fact("a3", RATE_NEW))
        self.live.generate_script.return_value = "unused"
        narrate([plain], listener(), self.live, heard_figures([plain], []))
        self.assertNotIn("recalls", self.live.generate_script.call_args.args[0])


class RefresherBriefingTest(unittest.TestCase):
    """The whole path: briefings are generated and remembered, then the next briefing recalls a figure."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        self.listener_id = "11111111-1111-4111-8111-111111111111"
        self.profile = {"name": "Listener", "topic_weights": {"rates": 1.0}, "language": "en",
                        "expertise": "intermediate", "duration": 3}
        self.old = fact("a1", RATE_OLD)
        self.new = fact("a2", RATE_NEW)

    def publish(self, *stories):
        self.store.replace_corpus([], list(stories), prepare_summaries(list(stories)))

    def brief(self, **fields):
        now = utc_now()
        spoken = self.store.spoken_fact_ids(self.listener_id, iso_utc(now - timedelta(days=7)), iso_utc(now))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            return generate_briefing(self.store, self.profile, listener_id=self.listener_id,
                                     spoken_fact_ids=spoken, **fields)

    def test_the_next_briefing_recalls_the_figure_the_listener_heard_and_remembers_both(self):
        self.publish(story("s1", self.old, thread="t", day="2026-11-17"))
        first = self.brief()
        self.assertNotIn("refresher_fact_ids", first["segments"][0])
        self.assertIn("Policy rate, 3.25 percent.", first["segments"][0]["text"])

        self.publish(story("s1", self.old, thread="t", day="2026-11-17"), story("s2", self.new, thread="t"))
        second = self.brief()
        segment, = second["segments"]
        self.assertEqual([item["id"] for item in second["stories"]], ["s2"])
        self.assertEqual(sentences(segment["text"])[1:], ["Last time, policy rate stood at 3.25 percent.", "Policy rate, 3.0 percent."])
        self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
        self.assertEqual(segment["fact_ids"], [self.new["id"], self.old["id"]])
        self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2", "a1"])
        self.assertEqual(second, self.store.briefing(second["id"]))
        now = utc_now()
        remembered = set(self.store.spoken_fact_ids(self.listener_id, iso_utc(now - timedelta(days=7)), iso_utc(now)))
        self.assertEqual(remembered, {self.old["id"], self.new["id"]})

        # Nothing new is left for this thread: the recalled figure is not replayed as news.
        with self.assertRaises(NoRelevantStories):
            self.brief()

    def test_a_third_value_recalls_the_one_heard_last_not_the_one_that_was_only_recalled(self):
        third = fact("a3", ("rate-third", "2.75", "The Freedonia central bank cut the policy rate to 2.75%."))
        corpus = [story("s1", self.old, thread="t", day="2026-11-16"), story("s2", self.new, thread="t", day="2026-11-17")]
        now = utc_now()
        self.publish(corpus[0])
        self.brief(available_at=iso_utc(now - timedelta(days=3)))
        self.publish(*corpus)
        # This briefing recalls 3.25 again, which records it a second time.
        self.assertIn("Last time, policy rate stood at 3.25 percent.", self.brief(available_at=iso_utc(now - timedelta(days=2)))["segments"][0]["text"])
        self.publish(*corpus, story("s3", third, thread="t", day="2026-11-18"))
        segment, = self.brief()["segments"]
        self.assertIn("Last time, policy rate stood at 3.0 percent.", segment["text"])
        self.assertNotIn("3.25", segment["text"])
        self.assertEqual(segment["refresher_fact_ids"], [self.new["id"]])

    def test_a_new_listener_hears_no_refresher(self):
        self.publish(story("s1", self.old, thread="t", day="2026-11-17"), story("s2", self.new, thread="t"))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            briefing = generate_briefing(self.store, self.profile, listener_id=self.listener_id)
        self.assertNotIn("Last time", " ".join(segment["text"] for segment in briefing["segments"]))
        self.assertTrue(all("refresher_fact_ids" not in segment for segment in briefing["segments"]))

    def test_a_fact_heard_longer_ago_than_the_window_is_not_recalled(self):
        unrelated = fact("a9", ("energy", "70", "The Freedonia station reported solar capacity of 70 megawatts."), unit="megawatts")
        self.publish(story("s1", self.old, self.new), story("s9", unrelated, day="2026-11-10"))
        now = utc_now()
        self.store.record_spoken_facts(self.listener_id, [self.old["id"]], "old-briefing", iso_utc(now - timedelta(days=10)))
        self.store.record_spoken_facts(self.listener_id, [unrelated["id"]], "recent-briefing", iso_utc(now - timedelta(days=1)))
        segment, = self.brief()["segments"]
        # Memory of the old figure has expired, so it is news again and nothing is recalled.
        self.assertIn("Policy rate, 3.25 percent. Policy rate, 3.0 percent.", segment["text"])
        self.assertNotIn("Last time", segment["text"])
        self.assertNotIn("refresher_fact_ids", segment)

    def test_a_recalled_fact_keeps_the_time_it_was_first_heard(self):
        self.store.record_spoken_facts(self.listener_id, [self.old["id"]], "b1", "2026-11-16T07:00:00Z")
        self.store.record_spoken_facts(self.listener_id, [self.old["id"], self.new["id"]], "b2", "2026-11-17T07:00:00Z")
        self.assertEqual(
            self.store.first_spoken_times(self.listener_id, [self.old["id"], self.new["id"], "other"]),
            {self.old["id"]: "2026-11-16T07:00:00Z", self.new["id"]: "2026-11-17T07:00:00Z"},
        )
        self.assertEqual(self.store.first_spoken_times("someone-else", [self.old["id"]]), {})


if __name__ == "__main__":
    unittest.main()
