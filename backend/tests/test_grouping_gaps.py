"""Grouping rules added for few-figure translations, one outlet's lookalikes, digests and lead-based overlap.

Every article here is synthetic; the names (Zorbo, Acme) and figures are invented.
"""
import unittest

from backend.app import pipeline
from backend.app.clustering import is_roundup, merge_same_event


def article(article_id, language, title, body, published, source="Invezz", tickers=()):
    return {"article_id": article_id, "language": language, "title": title, "body": body, "source": source,
            "url": f"https://example.invalid/{language}/{article_id}", "publication_date": published,
            "tickers": list(tickers)}


def groups(*articles):
    return sorted(sorted(a["article_id"] for a in group)
                  for group in pipeline.group_articles(list(articles), pipeline._group_key, pipeline._day))


EN = "Zorbo just handed Acme bulls a lifeline: read on"
DE = "Zorbo verschafft Acme-Bullen eine Atempause: Details"
SHORT_EN = "The board reshuffle lifted shares by 12 percent."
SHORT_DE = "Die Umbesetzung im Vorstand hob die Aktie um 12 Prozent."


def pair(**changes):
    """An English original and its German edition, with the German side adjusted by ``changes``."""
    german = dict(language="de", title=DE, body=SHORT_DE, published="2026-11-16T15:59:00Z", source="Invezz - German")
    german.update(changes)
    return [
        article("en", "en", EN, SHORT_EN, "2026-11-16T15:57:00Z"),
        article("de", german.pop("language"), german.pop("title"), german.pop("body"), german.pop("published"),
                german.pop("source")),
    ]


class FewFigureTranslationTest(unittest.TestCase):
    def test_a_short_translation_minutes_after_its_original_joins_it(self):
        self.assertEqual(groups(*pair()), [["de", "en"]])

    def test_a_short_translation_without_any_figures_joins_on_headline_words(self):
        english, german = pair(body="Die Umbesetzung im Vorstand hob die Aktie.")
        english["body"] = "The board reshuffle lifted shares."
        self.assertEqual(groups(english, german), [["de", "en"]])

    def test_the_evidence_it_needs_is_missing_or_too_weak_so_it_stays_separate(self):
        cases = {
            "another outlet": pair(source="Other - German"),
            "ten minutes later": pair(published="2026-11-16T16:07:00Z"),
            "one shared headline word": pair(title="Zorbo verschafft Bullen eine Atempause: Details"),
            "different figures": pair(body="Die Umbesetzung im Vorstand hob die Aktie um 45 Prozent."),
        }
        for name, articles in cases.items():
            with self.subTest(name):
                self.assertEqual(len(groups(*articles)), 2)

    def test_one_edition_per_language_per_original(self):
        english, first = pair(title="Zorbo verschafft Acme bulls eine Atempause: Details")
        second = article("de2", "de", "Acme: Zorbo verschafft Bullen Luft", SHORT_DE, "2026-11-16T15:58:00Z",
                         "Invezz - German")
        # de2 shares two headline words with the original, de shares three: only the better one joins.
        result = groups(english, first, second)
        self.assertEqual(result, [["de", "en"], ["de2"]])

    def test_an_edition_that_fits_two_originals_equally_is_not_guessed(self):
        english, german = pair()
        other = article("en2", "en", "Acme faces questions as Zorbo steps back", SHORT_EN, "2026-11-16T15:56:00Z")
        self.assertEqual(len(groups(english, german, other)), 3)

    def test_figure_rich_translations_still_match_on_figures_within_fifteen_minutes(self):
        rich_en = "Brent traded at $95.20 after rising 2.4% this week. Inventories grew by 3.1 million barrels."
        rich_de = "Brent notierte bei 95,20 $, nach einem Plus von 2,4 % in dieser Woche. Die Lagerbestände stiegen um 3,1 Millionen Barrel."
        result = groups(article("en", "en", "Zorbo crude near 95", rich_en, "2026-11-16T08:16:00Z"),
                        article("de", "de", "Zorbo-Rohöl nahe 95", rich_de, "2026-11-16T08:29:00Z", "Invezz - German"))
        self.assertEqual(result, [["de", "en"]])


FIGURES = "Levels were {}."


def figures(*numbers):
    return FIGURES.format(", ".join(str(n) for n in numbers))


class OneOutletLookalikeTest(unittest.TestCase):
    ANALYSIS = "Zorbo crude analysis: will crude fall below 90 as stockpiles swell"
    OUTLOOK = "Zorbo price outlook: will crude fall below 90 as stockpiles swell"

    def test_follow_up_with_a_lookalike_headline_a_day_later_is_a_separate_story(self):
        first = figures(11, 12, 13, 14, 15, 16, 17, 18)
        second = figures(11, 12, 13, 14, 15, 41, 42, 43)      # 5 of 8 figures shared
        self.assertEqual(len(groups(
            article("a", "en", self.ANALYSIS, first, "2026-11-16T08:00:00Z"),
            article("b", "en", self.OUTLOOK, second, "2026-11-17T07:00:00Z"),
        )), 2)

    def test_the_same_outlet_republishing_the_same_text_is_one_story_on_any_day(self):
        text = figures(11, 12, 13, 14, 15, 16, 17, 18)
        for published in ("2026-11-16T08:30:00Z", "2026-11-17T07:00:00Z"):
            with self.subTest(published=published):
                self.assertEqual(groups(
                    article("a", "en", self.ANALYSIS, text, "2026-11-16T08:00:00Z"),
                    article("b", "en", self.OUTLOOK, text, published),
                ), [["a", "b"]])

    def test_templated_headlines_with_different_lists_stay_apart_even_on_one_day(self):
        first = figures(11, 12, 13, 14, 15, 16, 17, 18)
        second = figures(11, 12, 13, 14, 15, 16, 51, 52)      # 75% shared, as screens that reuse metrics do
        pick = lambda title, body, hour: article(title, "en", title, body, f"2026-06-09T{hour}:00:00Z", "Screens")
        self.assertEqual(len(groups(
            pick("New Strong Buy Funds for June 9th", first, "08"),
            pick("Best Momentum Funds to Buy for June 9th", second, "13"),
        )), 2)
        self.assertEqual(len(groups(
            pick("New Strong Buy Funds for June 9th", first, "08"),
            pick("Best Momentum Funds to Buy for June 9th", first, "13"),
        )), 1)

    def test_another_outlet_covering_it_a_day_later_still_matches_on_half_the_figures(self):
        first = figures(11, 12, 13, 14, 15, 16, 17, 18)
        second = figures(11, 12, 13, 14, 15, 41, 42, 43)
        self.assertEqual(len(groups(
            article("a", "en", self.ANALYSIS, first, "2026-11-16T08:00:00Z", "Invezz"),
            article("b", "en", self.OUTLOOK, second, "2026-11-17T07:00:00Z", "IBTimes"),
        )), 1)

    def test_short_reports_on_one_day_still_group_but_not_across_days(self):
        short = "Operating profit was 610 million euros."
        title = "Arden reports operating profit"
        self.assertEqual(len(groups(article("a", "en", title, short, "2026-11-17T09:00:00Z"),
                                    article("b", "en", title, short, "2026-11-17T11:00:00Z"))), 1)
        self.assertEqual(len(groups(article("a", "en", title, short, "2026-11-17T09:00:00Z"),
                                    article("b", "en", title, short, "2026-11-18T09:00:00Z"))), 2)


def story_group(identifier, source, published, body, language="en", title=None):
    return [article(identifier, language, title or identifier, body, published, source)]


class CrossOutletMergeTest(unittest.TestCase):
    VECTORS = [[1.0, 0.0], [0.95, 0.05]]
    BODY = figures(11, 12, 13, 14, 15, 16, 17, 18)

    def merged(self, first, second, vectors=None):
        return len(merge_same_event([first, second], vectors or self.VECTORS))

    def test_digests_never_merge_with_a_single_story(self):
        for title in ("Evening digest: Zorbo returns, gold falls", "Wall Street futures: 5 things to know before the open",
                      "Stock Market News for Nov 18, 2026"):
            with self.subTest(title):
                self.assertTrue(is_roundup(article("x", "en", title, "", "2026-11-16T08:00:00Z")))
                self.assertEqual(self.merged(
                    story_group("a", "Invezz", "2026-11-16T08:00:00Z", self.BODY, title=title),
                    story_group("b", "IBTimes", "2026-11-16T09:00:00Z", self.BODY),
                ), 2)
        self.assertEqual(self.merged(story_group("a", "Invezz", "2026-11-16T08:00:00Z", self.BODY, title="Zorbo falls"),
                                     story_group("b", "IBTimes", "2026-11-16T09:00:00Z", self.BODY)), 1)
        self.assertFalse(is_roundup(article("x", "en", "Zorbo digests its acquisition", "", "2026-11-16T08:00:00Z")))

    def test_a_translation_does_not_inflate_the_overlap_of_its_group(self):
        lead = story_group("a", "Invezz", "2026-11-16T08:00:00Z", self.BODY)
        # The German edition repeats the original's figures but this one also carries three of the other
        # report's, so the group's union would overlap it by 5 of 8 although the leads share only 2 of 8.
        lead += story_group("a-de", "Invezz - German", "2026-11-16T08:02:00Z", figures(11, 12, 13, 14, 15, 16, 17, 18, 21, 22, 23), "de")
        other = story_group("b", "IBTimes", "2026-11-16T09:00:00Z", figures(11, 12, 21, 22, 23, 24, 25, 26))
        self.assertEqual(self.merged(lead, other), 2)

    def test_reports_within_a_day_and_a_half_merge_but_not_beyond(self):
        for later, expected in (("2026-11-17T14:00:00Z", 1), ("2026-11-18T00:00:00Z", 2)):     # 30 and 40 hours
            with self.subTest(later=later):
                self.assertEqual(self.merged(story_group("a", "Invezz", "2026-11-16T08:00:00Z", self.BODY),
                                             story_group("b", "IBTimes", later, self.BODY)), expected)


if __name__ == "__main__":
    unittest.main()
