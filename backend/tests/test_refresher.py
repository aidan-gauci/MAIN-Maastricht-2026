"""Refreshers: a figure heard within the memory window is never replayed as news, but may be recalled in one short
sentence ahead of a story with a new figure for the same measure. The sentence is carried beside the story's text
and has its own audio clip, so the story's clip stays shared. Synthetic stories only."""
import wave
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app import pipeline, service
from backend.app.audio import create_audio
from backend.app.pipeline import (
    _NARRATION_FRAMES, heard_figures, introduce_once, narrate, rank, spoken_text, story_segment,
)
from backend.app.providers import Providers
from backend.app.service import NoRelevantStories, SPOKEN_WORDS_PER_MINUTE, generate_briefing, iso_utc, utc_now
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


def refresh(corpus, target, spoken, language="en", providers=None, first_heard=None, recall=True, **fields):
    """Narrate ``target`` the way a returning listener hears it: heard facts removed, then (if ``recall``) recalled."""
    profile = listener(language, **fields)
    ranked = [row for row in rank(corpus, profile, spoken_fact_ids=spoken) if row["id"] == target["id"]]
    return narrate(ranked, profile, providers, heard_figures(corpus, spoken, first_heard) if recall else None)


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
                # The story reads exactly as it would without the recall; the recall is carried beside it.
                self.assertEqual(segment["text"], f"{_NARRATION_FRAMES[language]['intermediate']} {claim}")
                self.assertEqual(segment["refresher_text"], recall)
                # The recalled fact is cited with its own article, and marked as recalled.
                self.assertEqual(segment["fact_ids"], [self.new["id"], self.old["id"]])
                self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
                self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2", "a1"])
                self.assertEqual(segment["refresher_source_ids"], ["a1"])
                plain, = refresh([both], both, [self.old["id"]], language, recall=False)
                self.assertEqual(segment["text"], plain["text"])
                self.assertEqual(story_segment(segment), plain)

    def test_a_recalled_figure_from_an_article_already_cited_adds_no_source(self):
        same_article = fact("a1", RATE_NEW)
        both = story("s1", self.old, same_article)
        segment, = refresh([both], both, [self.old["id"]])
        self.assertEqual(segment["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
        self.assertEqual([source["article_id"] for source in segment["sources"]], ["a1"])
        self.assertEqual(segment["refresher_source_ids"], [])
        self.assertEqual(story_segment(segment), refresh([both], both, [self.old["id"]], recall=False)[0])

    def test_a_new_value_in_a_story_of_the_same_event_thread_recalls_it_with_its_own_source(self):
        earlier = story("s1", self.old, thread="thread-1", day="2026-11-17")
        later = story("s2", self.new, thread="thread-1")
        segment, = refresh([earlier, later], later, [self.old["id"]])
        self.assertEqual(sentences(segment["text"])[1:], ["Policy rate, 3.0 percent."])
        self.assertEqual(segment["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
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
                self.assertFalse([key for key in segment if key.startswith("refresher")])
                self.assertEqual(segment["fact_ids"], [current["facts"][0]["id"]])
                self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2"])

    def test_a_fact_outside_the_memory_window_or_a_new_listener_has_nothing_to_recall(self):
        both = story("s1", self.old, self.new)
        # The caller passes only the ids heard within the window, so an expired one never arrives.
        self.assertEqual(heard_figures([both], []), [])
        self.assertEqual(heard_figures([both], None), [])
        segment, = refresh([both], both, [])
        self.assertNotIn("refresher_text", segment)
        self.assertEqual(segment["fact_ids"], [self.old["id"], self.new["id"]])
        segment, = narrate([both], listener(), None)
        self.assertFalse([key for key in segment if key.startswith("refresher")])

    def test_a_heard_fact_that_is_no_longer_in_the_corpus_is_never_recalled(self):
        current = story("s2", self.new, thread="t")
        segment, = refresh([current], current, [self.old["id"]])
        self.assertNotIn("refresher_text", segment)
        self.assertEqual(heard_figures([current], [self.old["id"]]), [])

    def test_a_conflicted_claim_gets_no_refresher(self):
        conflicted = fact("a2", RATE_NEW, conflict_status="conflict", conflict_group="conflict-1")
        both = story("s1", self.old, conflicted)
        segment, = refresh([both], both, [self.old["id"]])
        self.assertNotIn("refresher_text", segment)

    def test_at_most_one_figure_is_recalled_per_segment_for_the_first_new_figure_it_gives_context_to(self):
        old = [self.old, fact("a1", INFLATION_OLD)]
        new = [self.new, fact("a2", INFLATION_NEW)]
        both = story("s1", *old, *new)
        spoken = [item["id"] for item in old]
        intro = _NARRATION_FRAMES["en"]["intermediate"]
        segment, = refresh([both], both, spoken)
        self.assertEqual(segment["text"], f"{intro} Policy rate, 3.0 percent. Inflation, 2.4 percent.")
        self.assertEqual(segment["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
        self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
        self.assertEqual(segment["fact_ids"], [self.new["id"], new[1]["id"], self.old["id"]])
        # Where the listener's interests put another figure first, that is the one given context.
        segment, = refresh([both], both, spoken, topic_weights={"inflation": 1.0})
        self.assertEqual(segment["text"], f"{intro} Inflation, 2.4 percent. Policy rate, 3.0 percent.")
        self.assertEqual(segment["refresher_text"], "Last time, inflation stood at 2.6 percent.")
        self.assertEqual(segment["refresher_fact_ids"], [old[1]["id"]])
        self.assertNotIn("Last time", segment["text"])
        self.assertEqual(len(sentences(segment["refresher_text"])), 1)

    def test_a_heard_figure_is_never_a_claim_again(self):
        both = story("s1", self.old, self.new)
        for language in ("en", "de", "nl"):
            with self.subTest(language=language):
                # Even handed a story that still holds the heard fact, only the recall says it.
                segment, = narrate([both], listener(language), None, heard_figures([both], [self.old["id"]]))
                self.assertNotRegex(segment["text"], r"3[.,]25")
                self.assertRegex(segment["refresher_text"], r"3[.,]25")
                self.assertTrue(segment["refresher_text"].startswith(RECALL[language].split("{}")[0]))
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
                self.assertEqual(segment["refresher_text"], f"Last time, policy rate stood at {expected} percent.")

    def test_the_introduction_and_headline_stay_with_the_story_and_the_recall_is_not_in_its_spoken_text(self):
        both = story("s1", self.old, self.new)
        first, = refresh([both], both, [self.old["id"]])
        briefing = introduce_once([first, dict(first, id="segment-s9")])
        intro = _NARRATION_FRAMES["en"]["intermediate"]
        self.assertEqual(briefing[0]["text"], f"{intro} Policy rate, 3.0 percent.")
        self.assertEqual(briefing[1]["text"], "Policy rate, 3.0 percent.")
        self.assertEqual(briefing[1]["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
        self.assertEqual(spoken_text(briefing[1], "Bank cuts rates", "en"), "Bank cuts rates. Policy rate, 3.0 percent.")


class LiveRefresherTest(unittest.TestCase):
    def setUp(self):
        old = [fact("a1", RATE_OLD), fact("a1", INFLATION_OLD)]
        new = [fact("a2", RATE_NEW), fact("a2", INFLATION_NEW)]
        self.story = story("s1", *old, *new)
        self.spoken = [item["id"] for item in old]
        self.offline, = refresh([self.story], self.story, self.spoken)
        self.intro, self.rate, self.inflation = sentences(self.offline["text"])
        self.live = Mock(spec=Providers, live=True)

    def narrate(self, draft, recall=True):
        self.live.generate_script.return_value = draft
        segment, = refresh([self.story], self.story, self.spoken, providers=self.live, recall=recall)
        return segment

    def test_the_model_arranges_the_story_without_seeing_or_touching_the_recall(self):
        reordered = f"{self.intro} {self.inflation} {self.rate}"
        segment = self.narrate(reordered)
        self.assertEqual(segment["text"], reordered)
        for key in ("refresher_text", "refresher_fact_ids", "refresher_source_ids", "fact_ids", "sources"):
            self.assertEqual(segment[key], self.offline[key])
        with_recall, = self.live.generate_script.call_args_list
        # The prompt is what it is without a recall: the heard figure never reaches the model.
        self.narrate(reordered, recall=False)
        self.assertEqual(with_recall.args, self.live.generate_script.call_args.args)
        self.assertNotIn("3.25", with_recall.args[0])
        self.assertNotIn("Last time", with_recall.args[0])

    def test_an_unsupported_draft_falls_back_to_the_verified_text_and_keeps_the_recall(self):
        for label, draft in (
            ("dropped", f"{self.intro} {self.rate}"),
            ("invented", f"{self.intro} {self.rate} {self.inflation} Prices are about to rise."),
            ("recall smuggled in", f"{self.intro} Last time, policy rate stood at 3.25 percent. {self.rate} {self.inflation}"),
        ):
            with self.subTest(label):
                self.assertEqual(self.narrate(draft), self.offline)


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

    def brief(self, listener_id=None, **fields):
        listener_id = listener_id or self.listener_id
        now = utc_now()
        spoken = self.store.spoken_fact_ids(listener_id, iso_utc(now - timedelta(days=7)), iso_utc(now))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            return generate_briefing(self.store, self.profile, listener_id=listener_id, spoken_fact_ids=spoken, **fields)

    def test_the_next_briefing_recalls_the_figure_the_listener_heard_and_remembers_the_new_one(self):
        self.publish(story("s1", self.old, thread="t", day="2026-11-17"))
        first = self.brief()
        self.assertFalse([key for key in first["segments"][0] if key.startswith("refresher")])
        self.assertIn("Policy rate, 3.25 percent.", first["segments"][0]["text"])

        self.publish(story("s1", self.old, thread="t", day="2026-11-17"), story("s2", self.new, thread="t"))
        second = self.brief()
        segment, = second["segments"]
        self.assertEqual([item["id"] for item in second["stories"]], ["s2"])
        self.assertEqual(sentences(segment["text"])[1:], ["Policy rate, 3.0 percent."])
        self.assertEqual(segment["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
        self.assertEqual(segment["refresher_fact_ids"], [self.old["id"]])
        self.assertEqual(segment["fact_ids"], [self.new["id"], self.old["id"]])
        self.assertEqual([source["article_id"] for source in segment["sources"]], ["a2", "a1"])
        self.assertEqual(second, self.store.briefing(second["id"]))
        # The length shown to the listener counts the recall too.
        words = len(f"{segment['refresher_text']} {segment['text']}".split())
        self.assertEqual(second["estimated_seconds"], round(words * 60 / SPOKEN_WORDS_PER_MINUTE))
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
        second = self.brief(available_at=iso_utc(now - timedelta(days=2)))["segments"][0]
        self.assertEqual(second["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
        self.publish(*corpus, story("s3", third, thread="t", day="2026-11-18"))
        segment, = self.brief()["segments"]
        self.assertEqual(segment["refresher_text"], "Last time, policy rate stood at 3.0 percent.")
        self.assertEqual(segment["refresher_fact_ids"], [self.new["id"]])

    def test_a_new_listener_hears_no_refresher(self):
        self.publish(story("s1", self.old, thread="t", day="2026-11-17"), story("s2", self.new, thread="t"))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            briefing = generate_briefing(self.store, self.profile, listener_id=self.listener_id)
        self.assertFalse([key for segment in briefing["segments"] for key in segment if key.startswith("refresher")])

    def test_a_fact_heard_longer_ago_than_the_window_is_not_recalled(self):
        unrelated = fact("a9", ("energy", "70", "The Freedonia station reported solar capacity of 70 megawatts."), unit="megawatts")
        self.publish(story("s1", self.old, self.new), story("s9", unrelated, day="2026-11-10"))
        now = utc_now()
        self.store.record_spoken_facts(self.listener_id, [self.old["id"]], "old-briefing", iso_utc(now - timedelta(days=10)))
        self.store.record_spoken_facts(self.listener_id, [unrelated["id"]], "recent-briefing", iso_utc(now - timedelta(days=1)))
        segment, = self.brief()["segments"]
        # Memory of the old figure has expired, so it is news again and nothing is recalled.
        self.assertIn("Policy rate, 3.25 percent. Policy rate, 3.0 percent.", segment["text"])
        self.assertNotIn("refresher_text", segment)

    def test_a_recalled_fact_keeps_the_time_it_was_first_heard(self):
        self.store.record_spoken_facts(self.listener_id, [self.old["id"]], "b1", "2026-11-16T07:00:00Z")
        self.store.record_spoken_facts(self.listener_id, [self.old["id"], self.new["id"]], "b2", "2026-11-17T07:00:00Z")
        self.assertEqual(
            self.store.first_spoken_times(self.listener_id, [self.old["id"], self.new["id"], "other"]),
            {self.old["id"]: "2026-11-16T07:00:00Z", self.new["id"]: "2026-11-17T07:00:00Z"},
        )
        self.assertEqual(self.store.first_spoken_times("someone-else", [self.old["id"]]), {})


def speak(text, path, language="en"):
    """A stand-in voice whose samples tell which text it spoke."""
    frames = (b"\x01\x00" * 8 if text.startswith(("Last time", "Zuletzt", "Vorige"))
              else b"\x03\x00" * 4 if text.endswith(tuple(pipeline._INTRODUCTIONS)) else b"\x02\x00" * 16)
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(frames)


REFRESHER_SAMPLES, STORY_SAMPLES, INTRO_SAMPLES = b"\x01\x00" * 8, b"\x02\x00" * 16, b"\x03\x00" * 4


class RefresherAudioTest(unittest.TestCase):
    """The recall is its own small clip ahead of the story's clip; the story's clip stays shared."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        self.old = fact("a1", RATE_OLD)
        self.new = fact("a2", RATE_NEW)
        self.listeners = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
        self.profile = {"name": "Listener", "topic_weights": {"rates": 1.0}, "language": "en",
                        "expertise": "intermediate", "duration": 3}

    def samples(self, url):
        with wave.open(str(self.store.audio_dir / url.rsplit("/", 1)[-1]), "rb") as audio:
            return audio.readframes(audio.getnframes())

    def segments(self, language="en"):
        both = story("s1", self.old, self.new)
        plain, = refresh([both], both, [self.old["id"]], language, recall=False)
        recalled, = refresh([both], both, [self.old["id"]], language)
        return plain, recalled

    def test_two_listeners_whose_briefings_differ_only_by_a_refresher_share_the_story_clip(self):
        plain, recalled = self.segments()
        self.assertEqual(story_segment(recalled), plain)
        self.assertIs(story_segment(plain), plain)
        with patch("backend.app.audio.offline_voice", side_effect=speak) as voice:
            without = create_audio({"language": "en", "stories": [], "segments": [plain]}, self.store, None, "demo")
            with_recall = create_audio({"language": "en", "stories": [], "segments": [recalled]}, self.store, None, "demo")
            again = create_audio({"language": "en", "stories": [], "segments": [recalled]}, self.store, None, "demo")

        # The opening line and the story are each rendered once; the second briefing only adds the small recall clip.
        introduction, body = pipeline._split_introduction(plain["text"])
        opening = pipeline.spoken_opening(introduction, "en")
        self.assertEqual([call.args[0] for call in voice.call_args_list], [opening, body, recalled["refresher_text"]])
        self.assertEqual(self.store.usage("demo")[0]["calls"], 3)
        self.assertEqual(with_recall, again)
        self.assertNotEqual(with_recall, without)
        # Both open with the shared line and end with the same story clip; the recall sits just before its story.
        self.assertEqual(self.samples(without), INTRO_SAMPLES + STORY_SAMPLES)
        self.assertEqual(self.samples(with_recall), INTRO_SAMPLES + REFRESHER_SAMPLES + STORY_SAMPLES)
        self.assertEqual(len(list(self.store.audio_dir.glob("*.wav"))), 5)  # three clips and two assembled briefings

    def test_the_story_clip_is_shared_whatever_the_recall_says(self):
        plain, recalled = self.segments()
        other = dict(recalled, refresher_text="Last time, policy rate stood at 3.5 percent.",
                     refresher_fact_ids=["fact-a7-other"], refresher_source_ids=["a7"],
                     fact_ids=[self.new["id"], "fact-a7-other"],
                     sources=[*plain["sources"], {"article_id": "a7", "title": "Other", "source": "Test Wire", "url": "https://example.invalid/a7"}])
        self.assertEqual(story_segment(other), plain)
        with patch("backend.app.audio.offline_voice", side_effect=speak) as voice:
            for segment in (plain, recalled, other):
                create_audio({"language": "en", "stories": [], "segments": [segment]}, self.store, None, "demo")
        self.assertEqual(voice.call_count, 4)  # the opening line, one story clip and two different recalls
        body = pipeline._split_introduction(plain["text"])[1]
        self.assertEqual(sum(call.args[0] == body for call in voice.call_args_list), 1)

    def test_generated_briefings_of_two_listeners_share_the_story_clip(self):
        listener_a, listener_b = self.listeners
        corpus = (story("s1", self.old, thread="t", day="2026-11-17"), story("s2", self.new, thread="t"))
        self.store.replace_corpus([], list(corpus), prepare_summaries(list(corpus)))
        self.store.record_spoken_facts(listener_b, [self.old["id"]], "earlier", iso_utc(utc_now() - timedelta(days=1)))
        now = utc_now()

        def briefing(listener_id):
            spoken = self.store.spoken_fact_ids(listener_id, iso_utc(now - timedelta(days=7)), iso_utc(now))
            return generate_briefing(self.store, self.profile, listener_id=listener_id, spoken_fact_ids=spoken)

        with patch.object(service, "MODE", "demo"), patch("backend.app.audio.offline_voice", side_effect=speak) as voice:
            new, returning = briefing(listener_a), briefing(listener_b)

        self.assertEqual((new["audio_status"], returning["audio_status"]), ("ready", "ready"))
        story_a, = new["segments"]
        story_b, = returning["segments"]
        self.assertNotIn("refresher_text", story_a)
        self.assertEqual(story_b["refresher_text"], "Last time, policy rate stood at 3.25 percent.")
        self.assertEqual(story_segment(story_b), story_a)
        # The story clip (introduction, headline, figures) is rendered once, for the first listener; the second
        # listener's briefing only adds the small recall clip.
        introduction, body = pipeline._split_introduction(story_a["text"])
        self.assertEqual([call.args[0] for call in voice.call_args_list],
                         [pipeline.spoken_opening(introduction, "en"),
                          spoken_text(dict(story_a, text=body), "Freedonia bank update", "en"), story_b["refresher_text"]])
        self.assertEqual(self.samples(new["audio_url"].split("?")[0]), INTRO_SAMPLES + STORY_SAMPLES)
        self.assertEqual(self.samples(returning["audio_url"].split("?")[0]), INTRO_SAMPLES + REFRESHER_SAMPLES + STORY_SAMPLES)

    def test_a_recall_is_spoken_in_the_story_voice_and_language_ahead_of_it(self):
        plain, recalled = self.segments("de")
        spoken = []

        def synthesize(text, path, language):
            spoken.append((text, language))
            Path(path).write_bytes(b"ID3" + b"\0" * 2000)

        counts = {}
        for voice in ("voice-a", "voice-b"):
            provider = SimpleNamespace(synthesize=synthesize, voice_id=lambda voice=voice: voice)
            with patch("backend.app.audio.shutil.which", return_value="ffmpeg"), patch(
                "backend.app.audio._assemble_audio",
                side_effect=lambda clips, output, ffmpeg: Path(output).write_bytes(b"ID3" + b"\0" * 4000),
            ):
                create_audio({"language": "de", "stories": [], "segments": [recalled]}, self.store, provider, "live")
            counts[voice] = len(spoken)
        introduction, body = pipeline._split_introduction(recalled["text"])
        self.assertEqual(spoken[:3], [(pipeline.spoken_opening(introduction, "de"), "de"),
                                      (recalled["refresher_text"], "de"), (body, "de")])
        self.assertEqual(counts, {"voice-a": 3, "voice-b": 6})
        # Another voice renders every clip again: nothing is shared across voices.
        self.assertEqual(len(list(self.store.audio_dir.glob("*.mp3"))), 8)  # three clips and one briefing per voice


if __name__ == "__main__":
    unittest.main()
