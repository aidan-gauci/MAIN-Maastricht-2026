import unittest

from backend.app import pipeline


def article(article_id, language, title, body, published, source="Wire"):
    return {"article_id": article_id, "language": language, "title": title, "body": body, "source": source,
            "url": f"https://example.invalid/{language}/{article_id}", "publication_date": published}


EN_BODY = "Brent traded at $95.20 after rising 2.4% this week. Inventories grew by 3.1 million barrels; OPEC output was 26.8 million."
DE_BODY = "Brent notierte bei 95,20 $, nach einem Plus von 2,4 % in dieser Woche. Die Lagerbestände stiegen um 3,1 Millionen Barrel; die OPEC förderte 26,8 Millionen."
OTHER_BODY = "Index futures rose 0.8% while the 10-year yield eased to 4.12%. Payrolls added 175,000 jobs; wages grew 3.9%."


def groups(*articles):
    return [sorted(a["article_id"] for a in group) for group in pipeline.group_articles(list(articles), pipeline._group_key, pipeline._day)]


class TranslationGroupingTest(unittest.TestCase):
    def test_translation_published_minutes_later_with_the_same_figures_joins_the_original(self):
        result = groups(
            article("en", "en", "Oil prices near $95 as supply glut grows", EN_BODY, "2026-11-16T08:16:00Z"),
            article("de", "de", "Ölpreise nahe 95 $: Überangebot wächst", DE_BODY, "2026-11-16T08:18:00Z"),
        )
        self.assertEqual(result, [["de", "en"]])

    def test_same_figures_hours_apart_or_different_figures_stay_separate(self):
        self.assertEqual(len(groups(
            article("en", "en", "Oil prices near $95 as supply glut grows", EN_BODY, "2026-11-16T08:16:00Z"),
            article("de", "de", "Ölpreise nahe 95 $: Überangebot wächst", DE_BODY, "2026-11-16T11:30:00Z"),
        )), 2)
        self.assertEqual(len(groups(
            article("en", "en", "Oil prices near $95 as supply glut grows", EN_BODY, "2026-11-16T08:16:00Z"),
            article("de", "de", "Märkte im Überblick", OTHER_BODY, "2026-11-16T08:18:00Z"),
        )), 2)


class TemplatedHeadlineTest(unittest.TestCase):
    def test_lookalike_template_headlines_with_different_figures_are_not_merged(self):
        result = groups(
            article("a", "en", "Stock Market News for Nov 17, 2026", EN_BODY, "2026-11-17T09:00:00Z", "Zacks"),
            article("b", "en", "Stock Market News for Nov 18, 2026", OTHER_BODY, "2026-11-18T09:00:00Z", "Zacks"),
        )
        self.assertEqual(len(result), 2)

    def test_short_reports_that_disagree_on_one_figure_still_group_for_conflict_labelling(self):
        result = groups(
            article("a", "en", "Arden reports operating profit", "Operating profit was 610 million euros.", "2026-11-17T09:00:00Z"),
            article("b", "en", "Arden reports operating profit", "Operating profit was 590 million euros.", "2026-11-17T09:30:00Z", "Other"),
        )
        self.assertEqual(result, [["a", "b"]])


if __name__ == "__main__":
    unittest.main()
