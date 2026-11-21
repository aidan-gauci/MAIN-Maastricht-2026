"""German and Dutch briefings are first class: correct figures and units, headlines, voices and availability."""
import os
import re
import shutil
import subprocess
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import audio, config, main, service
from backend.app.audio import create_audio, offline_voice
from backend.app.config import DEMO, STYLES, Wildcard, load_profiles, supported_languages
from backend.app.importer import read_articles
from backend.app.pipeline import _NARRATION_FRAMES, localized_headline, narrate, prepare, spoken_text
from backend.app.storage import Store

ENGLISH_WORDS = re.compile(
    r"\b(?:percent|million|billion|crowns?|workers|jobs|applications|basis points?|megawatts|days|hours|years|rate|"
    r"growth|forecast|previous|revenue|profit|sources|reports?|here|the|and|conflicting|values|reported|measures|plain|terms)\b",
    re.I,
)
CONFLICT = {
    "de": "Die Berichte nennen unterschiedliche Werte.",
    "nl": "De berichten noemen verschillende waarden.",
}


def article(key, title, body, language="en", **fields):
    return {
        "article_id": key, "source": f"{key} Journal", "url": f"https://{key}.example/story",
        "publication_date": "2026-11-19", "language": language, "title": title, "body": body, **fields,
    }



def listener(language="de", expertise="intermediate", **fields):
    return {"language": language, "expertise": expertise, "duration": 8, **fields}


def claims(text, language, expertise="intermediate"):
    """The sentences of a narration after its introduction."""
    intro = _NARRATION_FRAMES[language][expertise]
    assert text.startswith(intro + " "), text
    return re.split(r"(?<=\.)\s+", text[len(intro) + 1:])


class FixtureNarrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stories = prepare(read_articles(DEMO / "articles.jsonl"), None)

    def story(self, article_id):
        return next(story for story in self.stories if article_id in {a["article_id"] for a in story["articles"]})

    def text(self, article_id, language, expertise="intermediate"):
        segment, = narrate([self.story(article_id)], listener(language, expertise), None)
        return segment["text"]

    def test_german_and_dutch_narration_has_no_english_labels_units_or_framing(self):
        for language in ("de", "nl"):
            for expertise in STYLES:
                for story in self.stories:
                    if not story["facts"]:
                        continue
                    with self.subTest(language=language, expertise=expertise, story=story["title"]):
                        text, = (s["text"] for s in narrate([story], listener(language, expertise), None))
                        self.assertNotRegex(text, ENGLISH_WORDS)

    def test_numbers_use_a_decimal_comma_and_never_an_english_thousands_comma(self):
        # "8,600" would be read as eight point six in German and Dutch.
        for language in ("de", "nl"):
            for story in self.stories:
                if story["facts"]:
                    with self.subTest(language=language, story=story["title"]):
                        text, = (s["text"] for s in narrate([story], listener(language), None))
                        self.assertNotRegex(text, r"\d,\d{3}\b")
                        self.assertNotRegex(text, r"\d\.\d")
        self.assertIn("Leitzins, 3,25 Prozent.", self.text("a02-en", "de"))
        self.assertIn("Beleidsrente, 3,25 procent.", self.text("a02-en", "nl"))
        self.assertIn("Arbeitnehmer, 8600.", self.text("a03-en", "de"))
        self.assertIn("Werknemers, 8600.", self.text("a03-en", "nl"))

    def test_each_expertise_level_has_its_own_introduction_in_each_language(self):
        for language in ("de", "nl"):
            frames = [_NARRATION_FRAMES[language][level] for level in STYLES]
            self.assertEqual(len(set(frames)), 3)
            self.assertFalse(set(frames) & {_NARRATION_FRAMES["en"][level] for level in STYLES})
            for level in STYLES:
                with self.subTest(language=language, expertise=level):
                    self.assertTrue(self.text("a02-en", language, level).startswith(_NARRATION_FRAMES[language][level] + " "))

    def test_units_currency_and_scales_are_translated_and_inflected(self):
        expected = {
            ("a06-en", "de"): ["Solarkapazität, 420 Megawatt.", "Vertragspreis, 48 Kronen pro Megawattstunde."],
            ("a06-en", "nl"): ["Zonnecapaciteit, 420 megawatt.", "Contractprijs, 48 kronen per megawattuur."],
            ("a07-en", "de"): ["Tourismuseinnahmen, 2,3 Milliarden Kronen.", "Übernachtungen, 18,2 Millionen."],
            ("a07-en", "nl"): ["Toeristische inkomsten, 2,3 miljard kronen.", "Overnachtingen, 18,2 miljoen."],
            ("a08-en", "de"): ["Kreditlinie, 40 Millionen Kronen.", "Kreditobergrenze, 500000 Kronen."],
            ("a08-en", "nl"): ["Kredietlijn, 40 miljoen kronen.", "Kredietplafond, 500000 kronen."],
            ("a14-en", "de"): ["Überfällige Agrarkredite, 5,4 Prozent.", "Überfälligkeitsgrenze, 30 Tage.",
                               "Überfällige Agrarkredite zuvor, 4,1 Prozent."],
            ("a14-en", "nl"): ["Achterstallige landbouwleningen, 5,4 procent.", "Achterstandsgrens, 30 dagen.",
                               "Achterstallige landbouwleningen eerder, 4,1 procent."],
            ("a02-en", "de"): ["Leitzins, 3,25 Prozent.", "Inflation, 2,6 Prozent.", "Zinsänderung, 25 Basispunkte."],
            ("a02-en", "nl"): ["Beleidsrente, 3,25 procent.", "Inflatie, 2,6 procent.", "Renteverandering, 25 basispunten."],
        }
        for (article_id, language), sentences in expected.items():
            with self.subTest(article=article_id, language=language):
                self.assertEqual(claims(self.text(article_id, language), language), sentences)

    def test_the_conflict_sentence_is_said_once_in_the_listeners_language(self):
        for language in ("de", "nl"):
            with self.subTest(language=language):
                sentences = claims(self.text("a04-en", language), language)
                self.assertEqual(sentences.count(CONFLICT[language]), 1)
                self.assertEqual(sentences[-1], CONFLICT[language])
        self.assertEqual(claims(self.text("a04-en", "de"), "de")[:3], [
            "Vertretene Arbeitsplätze, 12400.", "Anträge, 310.", "Beantragter Betrag, 96 Millionen Kronen.",
        ])
        self.assertEqual(claims(self.text("a04-en", "nl"), "nl")[:3], [
            "Vertegenwoordigde banen, 12400.", "Aanvragen, 310.", "Aangevraagd bedrag, 96 miljoen kronen.",
        ])


class FiguresReadFaithfullyTest(unittest.TestCase):
    def speak(self, language, body, output=None):
        story, = prepare([article("synthetic", "Briefing", body, language)], None)
        return story, narrate([story], listener(output or language), None)

    def test_scales_and_multiples_that_would_be_misread_are_not_spoken(self):
        for language, body in (
            ("de", "Der Umsatz betrug 2,5 Billionen Euro."),  # a German Billion is a trillion
            ("de", "Der Umsatz betrug 1 Billion Euro."),
            ("nl", "De omzet bedroeg 5 biljoen dollar."),
            ("de", "Die Inflation stieg um 3 Prozentpunkte."),
            ("nl", "De winst steeg met 3 procentpunt."),
            ("nl", "De winst was 3 keer hoger."),
            ("de", "Der Gewinn war 3 Mal höher."),
            ("de", "Die Zahl der Arbeitsplätze stieg auf 5 Tausend."),
            ("nl", "Het aantal banen steeg naar 5 duizend."),
        ):
            for output in ("en", "de", "nl"):
                with self.subTest(body=body, output=output):
                    story, segments = self.speak(language, body, output)
                    self.assertTrue(story["facts"])
                    self.assertEqual(segments, [])

    def test_german_milliarden_are_still_spoken_as_billions(self):
        for output, sentence in (("en", "Revenue, 2.5 billion euros."), ("de", "Umsatz, 2,5 Milliarden Euro."),
                                 ("nl", "Omzet, 2,5 miljard euro.")):
            with self.subTest(output=output):
                _, (segment,) = self.speak("de", "Der Umsatz betrug 2,5 Milliarden Euro.", output)
                self.assertEqual(segment["text"], f"{_NARRATION_FRAMES[output]['intermediate']} {sentence}")

    def test_currency_words_after_a_scale_are_kept_in_every_language(self):
        for language, body, expected in (
            ("de", "Der Gewinn betrug 450 Millionen Pfund.", {"de": "Gewinn, 450 Millionen Pfund.", "nl": "Winst, 450 miljoen pond."}),
            ("nl", "De winst bedroeg 450 miljoen pond.", {"de": "Gewinn, 450 Millionen Pfund.", "nl": "Winst, 450 miljoen pond."}),
            ("de", "Der Umsatz betrug 450 Millionen US-Dollar.", {"de": "Umsatz, 450 Millionen Dollar.", "nl": "Omzet, 450 miljoen dollar."}),
        ):
            for output, sentence in expected.items():
                with self.subTest(body=body, output=output):
                    _, (segment,) = self.speak(language, body, output)
                    self.assertEqual(claims(segment["text"], output), [sentence])

    def test_singular_milliarde_and_basispunkt_keep_their_unit(self):
        # "1 Milliarde Euro" was spoken as a bare "1", and a single basis point as nothing at all.
        for language, body, expected in (
            ("de", "Der Umsatz betrug 1 Milliarde Euro.", {"en": "Revenue, 1 billion euros.", "de": "Umsatz, 1 Milliarde Euro.",
                                                              "nl": "Omzet, 1 miljard euro."}),
            ("de", "Der Leitzins stieg um 1 Basispunkt.", {"en": "Rate change, 1 basis point.", "de": "Zinsänderung, 1 Basispunkt.",
                                                            "nl": "Renteverandering, 1 basispunt."}),
            ("nl", "De rente steeg met 1 basispunt.", {"en": "Rate change, 1 basis point.", "de": "Zinsänderung, 1 Basispunkt.",
                                                               "nl": "Renteverandering, 1 basispunt."}),
        ):
            for output, sentence in expected.items():
                with self.subTest(body=body, output=output):
                    _, (segment,) = self.speak(language, body, output)
                    self.assertEqual(segment["text"], f"{_NARRATION_FRAMES[output]['intermediate']} {sentence}")

    def test_a_unit_agrees_with_the_number_as_it_is_spoken(self):
        # "1 Milliarde" but "1,0 Milliarden": the unit follows the written number, not its value.
        _, (single,) = self.speak("de", "Der Umsatz betrug 1 Milliarde Euro.")
        self.assertEqual(claims(single["text"], "de"), ["Umsatz, 1 Milliarde Euro."])
        _, (decimal,) = self.speak("de", "Der Umsatz betrug 1,0 Milliarden Euro.")
        self.assertEqual(claims(decimal["text"], "de"), ["Umsatz, 1,0 Milliarden Euro."])

    def test_english_separators_left_in_translated_text_are_not_misread(self):
        # Translated editions keep English formatting: "$70,000" would be heard as seventy, "$17.942 miljard"
        # as 17,942 billion. Such a figure is left out rather than guessed.
        for language, body in (
            ("de", "Der Umsatz fiel auf $70,000."),
            ("de", "Der Umsatz erreichte 1,234,567 Euro."),
            ("de", "Der Umsatz erreichte $1.216 million."),
            ("de", "Der Umsatz lag bei $0.963."),
            ("nl", "De omzet bedroeg $17.942 miljard."),
            ("nl", "De omzet bedroeg 1.708 miljoen."),
            ("nl", "Het aantal werknemers steeg naar 12,600."),
        ):
            for output in ("en", "de", "nl"):
                with self.subTest(body=body, output=output):
                    story, segments = self.speak(language, body, output)
                    self.assertTrue(story["facts"])
                    self.assertEqual(segments, [])

    def test_the_english_edition_supplies_a_figure_its_translations_would_have_garbled(self):
        english = article("btc-en", "Bitcoin slips", "Revenue fell to $70,000 in the quarter.", "en", url="https://news.example/en/btc")
        german = article("btc-de", "Bitcoin rutscht ab", "Der Umsatz fiel im Quartal auf $70,000.", "de", url="https://news.example/de/btc")
        story, = prepare([english, german], None)
        self.assertEqual({a["language"] for a in story["articles"]}, {"en", "de"})
        for output, sentence in (("en", "Revenue, 70,000 dollars."), ("de", "Umsatz, 70000 Dollar."), ("nl", "Omzet, 70000 dollar.")):
            with self.subTest(output=output):
                segment, = narrate([story], listener(output), None)
                self.assertEqual(segment["text"], f"{_NARRATION_FRAMES[output]['intermediate']} {sentence}")
                self.assertEqual({source["article_id"] for source in segment["sources"]}, {"btc-en"})

    def test_german_style_thousands_and_leading_zero_decimals_are_still_spoken(self):
        for language, body, expected in (
            ("de", "Der Umsatz fiel auf $76.000.", {"de": "Umsatz, 76.000 Dollar.", "en": "Revenue, 76000 dollars."}),
            ("de", "Der Umsatz lag bei $0,963.", {"de": "Umsatz, 0,963 Dollar.", "en": "Revenue, 0.963 dollars."}),
            ("nl", "Het aantal banen steeg naar 165.000.", {"nl": "Banen, 165.000.", "de": "Arbeitsplätze, 165.000."}),
        ):
            for output, sentence in expected.items():
                with self.subTest(body=body, output=output):
                    _, (segment,) = self.speak(language, body, output)
                    self.assertEqual(segment["text"], f"{_NARRATION_FRAMES[output]['intermediate']} {sentence}")

    def test_german_thousands_separators_are_not_read_as_decimals(self):
        _, (segment,) = self.speak("de", "Die Zahl der Arbeitsplätze lag bei 12.400, davon 8.600 Arbeitnehmer.", "nl")
        self.assertEqual(claims(segment["text"], "nl"), ["Banen, 12.400.", "Werknemers, 8.600."])
        _, (segment,) = self.speak("de", "Die Zahl der Arbeitsplätze lag bei 12.400, davon 8.600 Arbeitnehmer.", "en")
        self.assertEqual(segment["text"], f"{_NARRATION_FRAMES['en']['intermediate']} Jobs, 12400. Workers, 8600.")


class HeadlineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stories = prepare(read_articles(DEMO / "articles.jsonl"), None)

    def story(self, article_id):
        return next(story for story in self.stories if article_id in {a["article_id"] for a in story["articles"]})

    def test_the_headline_comes_from_the_edition_in_the_listeners_language(self):
        rate = self.story("a02-en")
        self.assertEqual(localized_headline(rate, "en"), (rate["title"], None))
        self.assertEqual(localized_headline(rate, "de"), ("Zentralbank hält Leitzins bei 3,25 Prozent", "de"))
        self.assertEqual(localized_headline(rate, "nl"), ("Centrale bank houdt rente op 3,25 procent", "nl"))

    def test_without_an_edition_in_that_language_no_language_is_claimed_for_the_title(self):
        wages = self.story("a03-en")
        for language in ("de", "nl"):
            self.assertEqual(localized_headline(wages, language), (wages["title"], None))
        self.assertEqual(localized_headline({"title": "Titel", "articles": [{"title": "", "language": "de"}]}, "de"), ("Titel", None))

    def test_only_a_headline_in_the_listeners_language_is_read_out(self):
        segment = {"id": "segment-a", "text": "Das berichten die Quellen. Leitzins, 3,25 Prozent."}
        spoken = spoken_text(segment, "Zentralbank hält Leitzins bei 3,25 Prozent", "de", "de")
        self.assertEqual(spoken, "Das berichten die Quellen. Zentralbank hält Leitzins bei 3,25 Prozent. Leitzins, 3,25 Prozent.")
        # An English title, or one of unknown language, is never read in a German briefing.
        self.assertEqual(spoken_text(segment, "Central bank holds rate", "de"), segment["text"])
        self.assertEqual(spoken_text(segment, "Central bank holds rate", "de", "en"), segment["text"])
        self.assertEqual(spoken_text(segment, "Centrale bank houdt rente", "de", "nl"), segment["text"])
        # English briefings announce the story title as before.
        english = {"id": "segment-a", "text": "Policy rate, 3.25 percent."}
        self.assertEqual(spoken_text(english, "Central bank holds rate", "en"), "Central bank holds rate. Policy rate, 3.25 percent.")


class BriefingLanguageTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory)
        self.store = Store(path / "test.sqlite3", path / "audio")
        self.store.initialize()
        self.stories = service.prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))
        self.rate_story = next(s["id"] for s in self.stories if "a02-en" in {a["article_id"] for a in s["articles"]})

    def profile(self, language):
        # Interested in every topic, so every story with something to say is eligible.
        return dict(
            id="all-topics", name="All topics", topic_weights=dict.fromkeys(service.list_topics(self.stories), 1.0),
            excluded_topics=[], expertise="intermediate", language=language, duration=8, style="neutral",
        )

    def test_german_and_dutch_briefings_head_stories_with_their_own_edition_titles(self):
        with patch.object(service, "create_audio", return_value="/api/audio/test.wav"):
            german = service.generate_briefing(self.store, self.profile("de"), persist=False)
            dutch = service.generate_briefing(self.store, self.profile("nl"), persist=False)
            english = service.generate_briefing(self.store, self.profile("en"), persist=False)
        by_id = {language: {s["id"]: s for s in briefing["stories"]} for language, briefing in
                 (("de", german), ("nl", dutch), ("en", english))}
        self.assertEqual(german["language"], "de")
        self.assertEqual(by_id["de"][self.rate_story]["title"], "Zentralbank hält Leitzins bei 3,25 Prozent")
        self.assertEqual(by_id["de"][self.rate_story]["title_language"], "de")
        self.assertEqual(by_id["nl"][self.rate_story]["title"], "Centrale bank houdt rente op 3,25 procent")
        # A story with no German edition keeps its title as the heading, flagged as not German.
        untranslated = [s for s in by_id["de"].values() if s["id"] != self.rate_story and "title_language" not in s]
        self.assertTrue(untranslated)
        # English is unchanged: story titles, no language marker.
        self.assertTrue(all("title_language" not in s for s in by_id["en"].values()))
        self.assertEqual(by_id["en"][self.rate_story]["title"], "Central bank holds rate, flags sticky services prices")

    def test_the_voice_reads_german_headlines_with_a_german_voice_and_nothing_for_untranslated_stories(self):
        spoken = []
        with patch.object(audio, "offline_voice", side_effect=lambda text, path, language: spoken.append((text, language))
                          or audio_stub(path)):
            briefing = service.generate_briefing(self.store, self.profile("de"), persist=False)
        by_segment = {"segment-" + s["id"]: s for s in briefing["stories"]}
        # The opening line is its own clip; every other clip is one story.
        # It says first that the voice is synthetic and that this is not investment advice.
        introductions = tuple(_NARRATION_FRAMES["de"].values())
        opening, = [text for text, _ in spoken if text.endswith(introductions)]
        self.assertTrue(opening.endswith("Das berichten die Quellen."))
        self.assertIn("KI-Stimme", opening)
        self.assertIn("keine Anlageberatung", opening)
        clips = [text for text, _ in spoken if not text.endswith(introductions)]
        texts = {segment["id"]: text for segment, text in zip(briefing["segments"], clips)}
        self.assertEqual({language for _, language in spoken}, {"de"})
        self.assertEqual(briefing["audio_status"], "ready")
        rate = texts["segment-" + self.rate_story]
        self.assertIn(" Zentralbank hält Leitzins bei 3,25 Prozent. ", " " + rate + " ")
        for segment in briefing["segments"]:
            story = by_segment[segment["id"]]
            if "title_language" not in story:
                # No English headline in a German briefing: the clip is the story text alone.
                self.assertEqual(texts[segment["id"]], segment["text"].removeprefix("Das berichten die Quellen.").strip())
            else:
                self.assertIn(story["title"], texts[segment["id"]])
                self.assertNotIn(story["title"], segment["text"])  # the transcript shows it as a heading instead

    def test_english_audio_still_uses_the_original_two_argument_voice_call(self):
        calls = []
        with patch.object(audio, "offline_voice", side_effect=lambda *args: calls.append(args) or audio_stub(args[1])):
            service.generate_briefing(self.store, self.profile("en"), persist=False)
        self.assertTrue(calls)
        self.assertTrue(all(len(args) == 2 for args in calls))


def audio_stub(path):
    with wave.open(str(path), "wb") as stub:
        stub.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stub.writeframes(b"\0\0" * 800)


class OfflineVoiceTest(unittest.TestCase):
    def run_voice(self, language, *, failures=(), which="C:/tools/espeak-ng"):
        path = Path("narration.wav")
        results = [subprocess.CalledProcessError(1, "espeak-ng") if failed else subprocess.CompletedProcess([], 0)
                   for failed in failures] or [subprocess.CompletedProcess([], 0)]
        with patch.object(audio.shutil, "which", return_value=which), \
                patch.object(audio.subprocess, "run", side_effect=results) as run:
            offline_voice("Hallo", path, language)
        return [call.args[0] for call in run.call_args_list]

    def test_espeak_gets_the_voice_of_the_briefing_language(self):
        for language, voice in (("en", "en"), ("de", "de"), ("nl", "nl"), ("DE", "de")):
            with self.subTest(language=language):
                [command] = self.run_voice(language)
                self.assertEqual(command[command.index("-v") + 1], voice)

    def test_an_unsupported_language_or_a_missing_voice_falls_back_to_english(self):
        [command] = self.run_voice("fr")
        self.assertEqual(command[command.index("-v") + 1], "en")
        first, second = self.run_voice("de", failures=(True, False))
        self.assertEqual([c[c.index("-v") + 1] for c in (first, second)], ["de", "en"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_voice("nl", failures=(True, True))

    def test_a_timeout_is_not_retried(self):
        with patch.object(audio.shutil, "which", return_value="espeak-ng"), \
                patch.object(audio.subprocess, "run", side_effect=subprocess.TimeoutExpired("espeak-ng", 60)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                offline_voice("Hallo", Path("narration.wav"), "de")
        self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "System.Speech is a Windows voice")
    def test_system_speech_selects_an_installed_voice_for_the_language(self):
        with patch.object(audio.shutil, "which", return_value=None), \
                patch.object(audio.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            offline_voice("Hallo", Path("narration.wav"), "nl")
        command, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertEqual(env["BRIEFING_VOICE_LANGUAGE"], "nl")
        self.assertIn("SelectVoice", command[-1])
        # With no voice installed for the language the default voice is kept: SelectVoice is conditional.
        self.assertIn("if ($match)", command[-1])

    @unittest.skipUnless(shutil.which("espeak-ng") or shutil.which("espeak"), "eSpeak NG is not installed")
    def test_espeak_speaks_german_and_dutch_with_their_own_voices(self):
        with TemporaryDirectory() as directory:
            rendered = {}
            for language, text in (("en", "Policy rate, 3.25 percent."), ("de", "Leitzins, 3,25 Prozent."),
                                   ("nl", "Beleidsrente, 3,25 procent.")):
                path = Path(directory) / f"{language}.wav"
                offline_voice(text, path, language)
                with wave.open(str(path), "rb") as clip:
                    self.assertGreater(clip.getnframes(), 1000)
                rendered[language] = path.read_bytes()
            # The same German text spoken by the English voice sounds different from the German voice.
            english_voice = Path(directory) / "de-with-en-voice.wav"
            offline_voice("Leitzins, 3,25 Prozent.", english_voice, "en")
            self.assertNotEqual(english_voice.read_bytes(), rendered["de"])


class LanguageAvailabilityTest(unittest.TestCase):
    def test_german_and_dutch_are_offered_in_demo_and_live_mode_unless_switched_off(self):
        for mode in ("demo", "live"):
            for value, expected in (
                (None, ["en", "de", "nl"]), ("", ["en", "de", "nl"]), ("1", ["en", "de", "nl"]),
                ("0", ["en"]), ("false", ["en"]), (" Off ", ["en"]),
            ):
                environment = {key: val for key, val in os.environ.items() if key != "BRIEFING_MULTILINGUAL"}
                if value is not None:
                    environment["BRIEFING_MULTILINGUAL"] = value
                with self.subTest(mode=mode, value=value), patch.object(config, "MODE", mode), \
                        patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(supported_languages(), expected)


class LanguageApiTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory)
        self.store = Store(path / "test.sqlite3", path / "audio")
        self.store.initialize()
        self.stories = service.prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))
        self.enterContext(patch.object(main, "store", self.store))
        self.enterContext(patch.object(main, "_arm_local_schedule"))
        self.enterContext(patch.object(service, "create_audio", return_value="/api/audio/test.wav"))
        environment = {key: value for key, value in os.environ.items() if key != "BRIEFING_MULTILINGUAL"}
        self.enterContext(patch.dict(os.environ, {**environment, "BRIEFING_N8N_WEBHOOK_URL": ""}, clear=True))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.profile = load_profiles()[0]

    def test_all_three_languages_are_listed(self):
        self.assertEqual(self.client.get("/api/languages").json(), ["en", "de", "nl"])

    def test_a_saved_profile_and_a_wildcard_both_follow_the_chosen_language(self):
        for language in ("de", "nl"):
            profile = self.client.post("/api/briefings", json={"profile_id": self.profile.id, "language": language})
            wildcard = self.client.post("/api/briefings", json={
                "language": language,
                "wildcard": {"topics": [self.stories[0]["topics"][0]], "expertise": "advanced", "duration": 3, "language": language},
            })
            for response, expertise in ((profile, self.profile.expertise), (wildcard, "advanced")):
                with self.subTest(language=language, expertise=expertise):
                    self.assertEqual(response.status_code, 201, response.text)
                    body = response.json()
                    self.assertEqual(body["language"], language)
                    self.assertTrue(body["segments"][0]["text"].startswith(_NARRATION_FRAMES[language][expertise]))

    def test_the_chosen_language_wins_over_the_languages_in_the_request_body_and_profile(self):
        response = self.client.post("/api/briefings", json={
            "language": "nl",
            "wildcard": {"topics": [self.stories[0]["topics"][0]], "expertise": "beginner", "duration": 3, "language": "en"},
        })
        self.assertEqual(response.json()["language"], "nl")

    def test_a_schedule_can_use_german_or_dutch(self):
        settings = dict(profile_id=self.profile.id, region="all", language="de", cadence="daily", local_time="07:30",
                        weekday=None, timezone="Europe/Amsterdam", voice_preset="default")
        listener = "11111111-1111-4111-8111-111111111111"
        response = self.client.put(f"/api/listeners/{listener}/schedule", json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["schedule"]["language"], "de")

    def test_switching_multilingual_off_keeps_english_only(self):
        with patch.dict(os.environ, {"BRIEFING_MULTILINGUAL": "0"}):
            self.assertEqual(self.client.get("/api/languages").json(), ["en"])
            response = self.client.post("/api/briefings", json={"profile_id": self.profile.id, "language": "de"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(self.client.post("/api/briefings", json={"profile_id": self.profile.id}).status_code, 201)


if __name__ == "__main__":
    unittest.main()
