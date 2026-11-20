"""Spoken narration reads like a briefing: specific labels, correct units, one wording, honest reasons."""
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.app.config import DEMO, STYLES, Wildcard, load_profiles
from backend.app.importer import read_articles
from backend.app.pipeline import (
    _NARRATION_FRAMES, _canonical_number, _describe_evidence, _spoken_number, _unit_phrase, narrate, prepare, rank,
)
from backend.app.service import SPOKEN_WORDS_PER_MINUTE, generate_briefing, prepare_corpus, wildcard_profile
from backend.app.storage import Store
from backend.app.summaries import prepare_summaries

GENERIC = re.compile(r"\breported measure\b|\bberichtete Kennzahl\b|\bgerapporteerd cijfer\b", re.I)
CONFLICT = {"The reports give conflicting values.", "Die Berichte nennen unterschiedliche Werte.", "De berichten noemen verschillende waarden."}


def article(key, title, body, language="en", **fields):
    return {
        "article_id": key, "source": f"{key} Journal", "url": f"https://{key}.example/story",
        "publication_date": "2026-11-19", "language": language, "title": title, "body": body, **fields,
    }


def listener(language="en", expertise="intermediate", **fields):
    return {"language": language, "expertise": expertise, "duration": 8, **fields}


def claims(segment, language="en", expertise="intermediate"):
    """The claim sentences of a segment, without its introduction."""
    intro = _NARRATION_FRAMES[language][expertise]
    assert segment["text"].startswith(intro + " "), segment["text"]
    return re.split(r"(?<=\.)\s+", segment["text"][len(intro) + 1:])


class FixtureNarrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stories = prepare(read_articles(DEMO / "articles.jsonl"), None)

    def story(self, article_id):
        return next(story for story in self.stories if article_id in {a["article_id"] for a in story["articles"]})

    def narrate(self, article_id, **fields):
        segment, = narrate([self.story(article_id)], listener(**fields), None)
        return segment

    def test_no_generic_labels_parentheses_or_verb_templates_in_any_language_or_expertise(self):
        for language in ("en", "de", "nl"):
            for expertise in STYLES:
                for story in self.stories:
                    if not story["facts"]:
                        continue
                    with self.subTest(language=language, expertise=expertise, story=story["title"]):
                        text, = (s["text"] for s in narrate([story], listener(language, expertise), None))
                        self.assertNotRegex(text, GENERIC)
                        self.assertNotIn("(", text)
                        self.assertNotRegex(text, r"\b(?:was|lag bei)\b")
                        for sentence in set(claims({"text": text}, language, expertise)) - CONFLICT:
                            # Every claim is "Label, amount." and an amount always names a number.
                            self.assertRegex(sentence, r"^[^,]+, \S.*\d.*\.$")

    def test_style_does_not_change_claim_wording(self):
        texts = {
            self.narrate("a05-en", expertise="beginner", style=style)["text"]
            for style in ("Use plain language and connect the story to household budgets",
                          "Lead with the signal and explain economic and market implications", "neutral")
        }
        self.assertEqual(texts, {
            f"{_NARRATION_FRAMES['en']['beginner']} Saving rate, 7.1 percent. "
            "Previous saving rate, 6.5 percent. Food prices, 3.8 percent."
        })

    def test_growth_forecast_keeps_each_figure_once_with_its_unit_across_translations(self):
        text = self.narrate("a01-en")["text"]
        self.assertEqual(claims({"text": text}), ["Growth forecast, 1.4 percent.", "Previous growth forecast, 1.8 percent."])
        # The German and Dutch editions say "von 1,8 auf 1,4 Prozent": one unit for both figures.
        for language, sentences in (
            ("de", {"Wachstumsprognose, 1,4 Prozent.", "Wachstumsprognose zuvor, 1,8 Prozent."}),
            ("nl", {"Groeiverwachting, 1,4 procent.", "Groeiverwachting eerder, 1,8 procent."}),
        ):
            with self.subTest(language=language):
                segment = self.narrate("a01-en", language=language)
                self.assertCountEqual(claims(segment, language), sentences)
        # All three editions stay cited even though they add no sentence of their own.
        segment = self.narrate("a01-en")
        self.assertEqual({source["article_id"] for source in segment["sources"]}, {"a01-en", "a09-de", "a10-nl"})
        self.assertEqual(len(segment["fact_ids"]), 6)

    def test_equivalent_facts_are_spoken_once_in_every_output_language(self):
        for language, expected in (("en", "Policy rate, 3.25 percent."), ("de", "Leitzins, 3,25 Prozent."),
                                   ("nl", "Beleidsrente, 3,25 procent.")):
            with self.subTest(language=language):
                sentences = claims(self.narrate("a02-en", language=language), language)
                self.assertEqual(len(sentences), 3)
                self.assertIn(expected, sentences)

    def test_units_are_plural_complete_and_not_repeated(self):
        # Four claims at most; the two conflicting job counts always make the cut.
        factory = claims(self.narrate("a04-en"))
        self.assertEqual(factory, [
            "Jobs represented, 12,400.", "Applications, 310.", "Amount requested, 96 million crowns.",
            "Jobs represented, 14,100.", "The reports give conflicting values.",
        ])
        self.assertEqual(claims(self.narrate("a07-en")), ["Tourism receipts, 2.3 billion crowns.", "Overnight stays, 18.2 million."])
        self.assertEqual(claims(self.narrate("a06-en")), [
            "Solar capacity, 420 megawatts.", "Contract price, 48 crowns per megawatt-hour.",
        ])
        self.assertEqual(claims(self.narrate("a03-en")), ["Pay increase, 4.2 percent.", "Pay increase, 3.0 percent.", "Workers, 8,600."])

    def test_labels_name_the_figure_closest_to_them(self):
        # "applications" and "loans" sit near figures that belong to something else.
        self.assertEqual(claims(self.narrate("a08-en")), ["Credit line, 40 million crowns.", "Loan cap, 500,000 crowns."])
        self.assertEqual(claims(self.narrate("a14-en")), [
            "Overdue farm loans, 5.4 percent.", "Overdue threshold, 30 days.", "Previous overdue farm loans, 4.1 percent.",
        ])

    def test_figures_without_a_nameable_measure_are_not_spoken_or_cited(self):
        story = self.story("a07-en")
        segment = self.narrate("a07-en")
        spoken = {fact["value"] for fact in story["facts"] if fact["id"] in segment["fact_ids"]}
        self.assertEqual(spoken, {"2.3", "18.2"})
        self.assertNotIn("6 percent", segment["text"])
        self.assertNotIn("4 percent", segment["text"])

    def test_live_narration_still_accepts_a_reordering_of_the_verified_sentences(self):
        segment = self.narrate("a01-en")
        intro = _NARRATION_FRAMES["en"]["intermediate"]
        first, second = claims(segment)
        live = Mock(live=True)
        live.generate_script.return_value = f"{intro} {second} {first}"
        reordered, = narrate([self.story("a01-en")], listener(), live)
        self.assertEqual(reordered["text"], f"{intro} {second} {first}")
        live.generate_script.return_value = f"{intro} Growth forecast, 1.9 percent. {second}"
        fallback, = narrate([self.story("a01-en")], listener(), live)
        self.assertEqual(fallback, segment)


class SyntheticNarrationTest(unittest.TestCase):
    def narrate(self, body, title="Briefing", **fields):
        story, = prepare([article("synthetic", title, body)], None)
        segment, = narrate([story], listener(**fields), None)
        return story, segment

    def test_a_figure_with_no_nameable_measure_is_read_plainly_not_as_a_reported_measure(self):
        story, segment = self.narrate("Exports came to 6% in the period.")
        self.assertEqual(segment["text"], f"{_NARRATION_FRAMES['en']['intermediate']} The report gives 6 percent.")
        self.assertEqual(segment["fact_ids"], [story["facts"][0]["id"]])

    def test_unnamed_figures_are_dropped_when_another_figure_in_the_story_has_a_name(self):
        story, segment = self.narrate("Exports came to 6% in the period. Profit reached 40 million euros.")
        self.assertEqual(claims(segment), ["Profit, 40 million euros."])
        named = next(fact for fact in story["facts"] if fact["value"] == "40")
        self.assertEqual(segment["fact_ids"], [named["id"]])

    def test_comparison_values_share_the_measure_of_the_figure_they_are_compared_with(self):
        _, segment = self.narrate("The saving rate rose to 7.1% in the quarter, up from 6.5% in the one before.")
        self.assertEqual(claims(segment), ["Saving rate, 7.1 percent.", "Previous saving rate, 6.5 percent."])

    def test_a_calendar_year_does_not_take_the_label_of_the_figure_beside_it(self):
        _, segment = self.narrate("The office cut its 2027 growth forecast to 1.4%.")
        self.assertEqual(claims(segment), ["Growth forecast, 1.4 percent."])

    def test_scale_words_and_rates_take_their_currency_from_the_sentence(self):
        _, segment = self.narrate("Operating profit was 610 million euros. Revenue reached 4.2 billion euros.")
        self.assertEqual(sorted(claims(segment)), ["Profit, 610 million euros.", "Revenue, 4.2 billion euros."])

    def test_single_units_are_singular(self):
        _, segment = self.narrate("The saving rate was 1% and the grant budget was 1 million crowns.")
        self.assertEqual(sorted(claims(segment)), ["Budget, 1 million crowns.", "Saving rate, 1 percent."])
        self.assertEqual(_unit_phrase("crown", "1", "en")[0], "crown")
        self.assertEqual(_unit_phrase("crown", "60", "en")[0], "crowns")

    def test_unit_words_follow_the_output_language(self):
        self.assertEqual(_unit_phrase("million crown", "60", "en")[0], "million crowns")
        self.assertEqual(_unit_phrase("billion euro", "4.2", "de")[0], "Milliarden Euro")
        self.assertEqual(_unit_phrase("basispunkte", "25", "nl")[0], "basispunten")
        self.assertEqual(_unit_phrase("crown per megawatt-hour", "48", "de")[0], "Kronen pro Megawattstunde")
        self.assertIsNone(_unit_phrase("tonnes", "5", "en"))

    def test_a_unit_shared_across_a_range_is_not_dropped_from_the_first_figure(self):
        evidence = "Das Büro senkte seine Wachstumsprognose von 1,8 auf 1,4 Prozent."
        for value, unit, label in (("1,8", "", "previous growth forecast"), ("1,4", "Prozent", "growth forecast")):
            with self.subTest(value=value):
                figure = _describe_evidence(evidence, value, unit, "de", "en")
                self.assertEqual((figure.label, figure.unit), (label, "percent"))


class TruthfulReasonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stories = prepare(read_articles(DEMO / "articles.jsonl"), None)

    def reasons(self, **weights):
        return {row["title"]: row["reason"] for row in rank(self.stories, {"topic_weights": weights})}

    def test_a_passing_mention_is_reported_as_a_mention_not_a_match(self):
        bank = next(story for story in self.stories if story["title"].startswith("Central bank"))
        self.assertIn("energy", bank["topics"])
        self.assertEqual(bank["passing_topics"].count("energy"), 1)
        reasons = self.reasons(energy=1.0)
        self.assertEqual(reasons[bank["title"]], "Mentions energy")
        self.assertEqual(reasons["Solar auction clears at lower price than spring round"], "Matches your interest in energy")

    def test_reasons_name_only_topics_the_story_is_about(self):
        reasons = self.reasons(inflation=1, housing=1, jobs=0.9, rates=0.8, energy=0.5)
        self.assertEqual(reasons["Central bank holds rate, flags sticky services prices"], "Matches your interest in inflation, rates")
        # "saving rate" and "food prices" are not an interest rate story.
        self.assertEqual(reasons["Households save more as food bills remain elevated"], "Matches your interest in housing")
        self.assertEqual(reasons["Harborland trims growth forecast as exports cool"], "Mentions housing")

    def test_topics_the_publisher_supplied_are_never_passing(self):
        row = article("bank", "Quarterly update", "Inflation stayed at 2.6% according to the energy regulator.")
        row["topics"] = ["energy"]
        story, = prepare([row], None)
        self.assertNotIn("energy", story.get("passing_topics", []))
        ranked, = rank([story], {"topic_weights": {"energy": 1}})
        self.assertEqual(ranked["reason"], "Matches your interest in energy")

    def test_a_topic_in_the_headline_or_repeated_in_the_text_is_not_passing(self):
        titled, = prepare([article("a", "Solar output grows", "Solar capacity reached 12 megawatts.")], None)
        repeated, = prepare([article("b", "Weekly update", "Energy output grew. The energy regulator said capacity reached 12 megawatts.")], None)
        self.assertNotIn("energy", titled.get("passing_topics", []))
        self.assertNotIn("energy", repeated.get("passing_topics", []))
        passing, = prepare([article("c", "Weekly update", "Inflation stayed at 2.6%, conditional on energy prices.")], None)
        self.assertEqual(passing["passing_topics"], ["energy"])


class EstimatedLengthTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))

    def test_the_briefing_reports_how_long_it_will_actually_run(self):
        profiles = [profile.model_dump() for profile in load_profiles()]
        profiles.append(wildcard_profile(Wildcard(topics=["energy"], expertise="intermediate", duration=8, language="en")))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            for profile in profiles:
                with self.subTest(profile=profile["id"]):
                    briefing = generate_briefing(self.store, profile)
                    words = sum(len(segment["text"].split()) for segment in briefing["segments"])
                    self.assertEqual(briefing["requested_minutes"], profile["duration"])
                    self.assertEqual(briefing["estimated_seconds"], round(words * 60 / SPOKEN_WORDS_PER_MINUTE))
                    self.assertEqual(briefing, self.store.briefing(briefing["id"]))
        # Narration is never padded to fill the requested length.
        self.assertLess(briefing["estimated_seconds"], briefing["requested_minutes"] * 60)

    def test_wildcard_briefing_has_specific_wording_and_truthful_reasons(self):
        profile = wildcard_profile(Wildcard(topics=["energy", "tourism"], expertise="beginner", duration=3, language="en"))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            briefing = generate_briefing(self.store, profile)
        text = " ".join(segment["text"] for segment in briefing["segments"])
        self.assertNotRegex(text, GENERIC)
        reasons = {story["title"]: story["reason"] for story in briefing["stories"]}
        self.assertEqual(reasons["Central bank holds rate, flags sticky services prices"], "Mentions energy")
        self.assertEqual(reasons["Tourism receipts recover, but visitor nights lag"], "Matches your interest in tourism")


class NumberMagnitudeTest(unittest.TestCase):
    def test_a_dot_is_a_thousands_separator_only_before_exactly_three_digits(self):
        for language in ("de", "nl"):
            for written, canonical in (
                ("3.31", "3.31"), ("3.4", "3.4"), ("9.1", "9.1"), ("0.5", "0.5"),
                ("1.234", "1234"), ("12.400", "12400"), ("1.234.567", "1234567"),
                ("3,31", "3.31"), ("1.234,5", "1234.5"), ("25", "25"),
            ):
                with self.subTest(language=language, written=written):
                    self.assertEqual(_canonical_number(written, language), canonical)
        # English keeps "," as the thousands separator and "." as the decimal point.
        self.assertEqual(_canonical_number("1,234.5", "en"), "1234.5")
        self.assertEqual(_canonical_number("3.31", "en"), "3.31")

    def test_spoken_numbers_keep_their_magnitude_in_every_language_pair(self):
        for value, source, output, spoken in (
            ("3.31", "de", "en", "3.31"), ("3.31", "de", "de", "3,31"), ("3.31", "nl", "nl", "3,31"),
            ("3,31", "de", "en", "3.31"), ("3,31", "de", "de", "3,31"), ("3.31", "en", "de", "3,31"),
            ("1.234", "de", "de", "1.234"), ("1.234", "de", "en", "1234"), ("8,600", "en", "en", "8,600"),
            ("3.50", "en", "en", "3.50"), ("1.234,5", "nl", "en", "1234.5"),
        ):
            with self.subTest(value=value, source=source, output=output):
                self.assertEqual(_spoken_number(value, source, output), spoken)

    def test_german_and_dutch_billions_are_not_spoken_a_hundred_times_too_large(self):
        for language, body, label, unit in (
            ("de", "Der Gewinn betrug 3.31 Milliarden Euro.", "Gewinn", "Milliarden Euro"),
            ("nl", "De winst bedroeg 3.4 miljard euro.", "Winst", "miljard euro"),
        ):
            story, = prepare([article("src", "Bilanz", body, language)], None)
            segment, = narrate([story], listener("en"), None)
            number = "3.31" if language == "de" else "3.4"
            self.assertEqual(claims(segment), [f"Profit, {number} billion euros."])
            segment, = narrate([story], listener(language), None)
            self.assertEqual(claims(segment, language), [f"{label}, {number.replace('.', ',')} {unit}."])


class UnderstoodFiguresOnlyTest(unittest.TestCase):
    def narrate(self, body, **fields):
        story, = prepare([article("synthetic", "Briefing", body)], None)
        return story, narrate([story], listener(**fields), None)

    def test_scales_and_multiples_the_figure_pattern_does_not_know_are_never_spoken_as_bare_numbers(self):
        for body in (
            "Job growth reached 165k jobs.",
            "Revenue was $1.5 trillion last year.",
            "Revenue is valued at 18.5x earnings.",
            "Profit rose by 25 bps.",
            "Profit rose by 5 percentage points.",
            "Profit was 3 times higher.",
            "Gewinn 3,5 Mio. Euro.",
        ):
            with self.subTest(body=body):
                story, segments = self.narrate(body)
                self.assertTrue(story["facts"])
                self.assertEqual(segments, [])

    def test_an_understood_figure_is_still_spoken_beside_one_that_is_not(self):
        story, (segment,) = self.narrate("Job growth reached 165k jobs. Profit reached 12 million euros.")
        self.assertEqual(claims(segment), ["Profit, 12 million euros."])
        self.assertEqual(segment["fact_ids"], [next(f["id"] for f in story["facts"] if f["value"] == "12")])

    def test_ranges_are_not_split_into_two_claims(self):
        for body in (
            "The inflation target is 2-3%.",
            "The inflation target is 2–3%.",
            "The inflation target is between 2% and 3%.",
            "The inflation target is 2 to 3 percent.",
        ):
            with self.subTest(body=body):
                _, segments = self.narrate(body)
                self.assertEqual(segments, [])
        _, (segment,) = self.narrate("Inflation was 2.1% in March. The target is between 2% and 3%.")
        self.assertEqual(claims(segment), ["Inflation, 2.1 percent."])

    def test_a_change_to_a_level_is_not_mistaken_for_a_range(self):
        _, (segment,) = self.narrate("Revenue rose 5% to 4.2 billion euros.")
        self.assertEqual(claims(segment), ["Revenue, 4.2 billion euros."])

    def test_currency_symbols_name_the_currency(self):
        _, (segment,) = self.narrate("Profit reached $3.31 billion and revenue £5 million.")
        self.assertEqual(sorted(claims(segment)), ["Profit, 3.31 billion dollars.", "Revenue, 5 million pounds."])

    def test_a_story_with_nothing_speakable_has_no_summary_and_is_replaced_in_the_briefing(self):
        unspeakable = article("hiring", "Hiring jobs surge", "Job growth reached 165k jobs.")
        speakable = article("bank", "Central bank holds rate", "The bank kept its policy rate at 3.25 percent.")
        stories = prepare([unspeakable, speakable], None)
        self.assertEqual(set(prepare_summaries(stories)), {next(s["id"] for s in stories if s["title"].startswith("Central"))})
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "test.sqlite3", root / "audio")
            store.initialize()
            prepare_corpus(store, [unspeakable, speakable])
            profile = wildcard_profile(Wildcard(topics=["jobs", "rates"], expertise="intermediate", duration=3, language="en"))
            with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
                briefing = generate_briefing(store, profile)
        self.assertEqual([story["title"] for story in briefing["stories"]], ["Central bank holds rate"])
        self.assertIn("Policy rate, 3.25 percent.", briefing["segments"][0]["text"])


class ShortSpecificNarrationTest(unittest.TestCase):
    def narrate(self, body, **fields):
        story, = prepare([article("synthetic", "Briefing", body)], None)
        segment, = narrate([story], listener(**fields), None)
        return story, segment

    BODY = (
        "Revenue was 1 billion euros. Profit was 2 million euros. Inflation was 2.1%. The policy rate was 3.25%. "
        "The saving rate was 7.1%. Food prices were 3.8% higher."
    )

    def test_a_story_reads_at_most_four_claims_and_cites_only_those(self):
        story, segment = self.narrate(self.BODY)
        self.assertEqual(claims(segment), [
            "Revenue, 1 billion euros.", "Profit, 2 million euros.", "Inflation, 2.1 percent.", "Policy rate, 3.25 percent.",
        ])
        spoken = {fact["value"] for fact in story["facts"] if fact["id"] in segment["fact_ids"]}
        self.assertEqual(spoken, {"1", "2", "2.1", "3.25"})
        self.assertEqual(len(story["facts"]), 6)

    def test_the_choice_of_claims_is_the_same_in_every_language_and_expertise(self):
        story, = prepare([article("synthetic", "Briefing", self.BODY)], None)
        cited = {
            tuple(narrate([story], listener(language, expertise), None)[0]["fact_ids"])
            for language in ("en", "de", "nl") for expertise in STYLES
        }
        self.assertEqual(len(cited), 1)

    def test_named_figures_with_a_unit_come_before_bare_counts(self):
        _, segment = self.narrate(
            "The ministry said 12,400 manufacturing jobs were represented in applications. "
            + self.BODY.split(" The saving rate")[0]
        )
        self.assertNotIn("Jobs represented", segment["text"])
        self.assertEqual(len(claims(segment)), 4)

    def test_conflicting_values_always_make_the_cut(self):
        first = article("first", "Inflation outlook", "Profit was 2 million euros. Revenue was 1 billion euros. "
                        "Inflation was 2.1%. The saving rate was 7.1%. Food prices were 3.8% higher. The policy rate was 3.25%.",
                        url="https://wire.example/outlook")
        second = article("second", "Inflation outlook", "Inflation was 2.9%.", url="https://wire.example/outlook")
        story, = prepare([first, second], None)
        self.assertTrue(story["conflicts"])
        segment, = narrate([story], listener(), None)
        sentences = claims(segment)
        self.assertEqual(len(sentences), 5)
        self.assertEqual(sentences[-1], "The reports give conflicting values.")
        self.assertEqual(sum(sentence.startswith("Inflation") for sentence in sentences), 2)


class LabelTriggerTest(unittest.TestCase):
    def claims(self, body, language="en"):
        story, = prepare([article("synthetic", "Briefing", body, language)], None)
        segment, = narrate([story], listener(language), None)
        return claims(segment, language)

    def test_a_phrase_inside_a_hyphenated_compound_does_not_name_a_figure(self):
        self.assertEqual(
            self.claims("The cost-to-revenue ratio was 62 million euros. Revenue reached 4 billion euros."),
            ["Revenue, 4 billion euros."],
        )
        self.assertEqual(
            self.claims("Das Kurs-Gewinn-Verhältnis lag bei 18,5 Prozent. Der Gewinn betrug 3 Millionen Euro.", "de"),
            ["Gewinn, 3 Millionen Euro."],
        )

    def test_a_percentage_beside_revenue_or_profit_is_a_ratio_or_change_not_the_measure(self):
        self.assertEqual(self.claims("Costs were 62% of revenue. Revenue reached 4 billion euros."), ["Revenue, 4 billion euros."])
        self.assertEqual(self.claims("Profit rose 8% to 3 million euros."), ["Profit, 3 million euros."])

    def test_a_label_does_not_cross_a_clause_break(self):
        self.assertEqual(
            self.claims("The ratio was 18.5 while revenue reached 4 billion euros."), ["Revenue, 4 billion euros."],
        )


class SourceConflictTest(unittest.TestCase):
    def test_figures_within_one_article_are_not_conflicting_sources(self):
        for body in (
            "Inflation is projected at 2.6% in the baseline. Inflation reaches 4.1% in the adverse scenario.",
            "The bank projects inflation of 2.6% this year. It projects inflation of 3.2% next year.",
        ):
            with self.subTest(body=body):
                story, = prepare([article("only", "Inflation outlook", body)], None)
                self.assertEqual(story["conflicts"], [])
                self.assertTrue(all(fact["conflict_status"] == "supported" for fact in story["facts"]))
                segment, = narrate([story], listener(), None)
                self.assertFalse(segment["conflict"])
                self.assertNotIn("conflicting", segment["text"])

    def test_figures_from_different_articles_still_conflict(self):
        first = article("first", "Inflation outlook", "Inflation is projected at 2.6% this year.", url="https://wire.example/outlook")
        second = article("second", "Inflation outlook", "Inflation is projected at 4.1% this year.", url="https://wire.example/outlook")
        story, = prepare([first, second], None)
        self.assertEqual(len(story["conflicts"]), 1)
        segment, = narrate([story], listener(), None)
        self.assertTrue(segment["conflict"])

    def test_an_article_that_sets_a_figure_apart_as_another_source_still_conflicts(self):
        body = ("The ministry puts inflation at 2.6% this year. "
                "A separate estimate from the central bank puts inflation at 4.1% this year.")
        story, = prepare([article("only", "Inflation outlook", body)], None)
        self.assertEqual(len(story["conflicts"]), 1)


if __name__ == "__main__":
    unittest.main()
