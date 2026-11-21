"""Headlines and measure names translated for other listener languages: checked, cited, never a figure."""
import unittest

from backend.app import pipeline
from backend.app.summaries import prepare_summaries
from backend.app.translations import digits, translate_corpus, valid_headline, valid_measure


def _story():
    article = {"article_id": "a1", "title": "Central bank lifts its policy rate to 3.5%", "source": "Example Wire",
               "url": "https://example.test/a1", "language": "en"}
    evidence = "The central bank raised its policy rate to 3.5% on Thursday."
    return {
        "id": "s1", "title": article["title"], "articles": [article], "topics": ["rates"], "publication_date": "2026-05-01",
        "facts": [{"id": "f1", "article_id": "a1", "value": "3.5", "unit": "%", "evidence": evidence,
                   "measure": "central bank policy rate", "language": "en", "conflict_status": "supported"}],
    }


class _Translator:
    live = True

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def translate_labels(self, texts, source, targets):
        self.calls.append((list(texts), source, tuple(targets)))
        return self.reply(texts, targets) if callable(self.reply) else self.reply


def _faithful(texts, targets):
    known = {"Central bank lifts its policy rate to 3.5%": {"de": "Zentralbank hebt Leitzins auf 3,5 %", "nl": "Centrale bank verhoogt beleidsrente naar 3,5%"},
             "central bank policy rate": {"de": "Leitzins der Zentralbank", "nl": "beleidsrente van de centrale bank"}}
    return {language: [known[text][language] for text in texts] for language in targets}


class LabelTranslationTests(unittest.TestCase):
    def test_checks_compare_numbers_not_their_separators(self):
        self.assertEqual(digits("up 4.0% to 1,250"), digits("um 4,0 % auf 1.250"))
        self.assertEqual("Leitzins auf 3,5 %", valid_headline("Policy rate at 3.5%", "Leitzins auf 3,5 %"))
        self.assertEqual("", valid_headline("Policy rate at 3.5%", "Leitzins auf 3,75 %"))
        self.assertEqual("", valid_headline("Policy rate rises", "Leitzins steigt auf 4 %"))
        self.assertEqual("", valid_measure("policy rate", "Leitzins 3,5"))
        self.assertEqual("", valid_measure("share price", "Aktie kaufen"))
        self.assertEqual("Leitzins", valid_measure("policy rate", "Leitzins."))

    def test_one_request_per_story_translates_headline_and_measure_names(self):
        story, provider = _story(), _Translator(_faithful)
        report = translate_corpus([story], provider, ("en", "de", "nl"))
        self.assertEqual(1, len(provider.calls))
        self.assertEqual(("en", ("de", "nl")), provider.calls[0][1:])
        self.assertEqual({"stories": 1, "labels": 4, "failed_stories": 0}, report)
        self.assertEqual("Zentralbank hebt Leitzins auf 3,5 %", story["headlines"]["de"])
        self.assertEqual("Leitzins der Zentralbank", story["facts"][0]["measure_translations"]["de"])
        # Asking again costs nothing: only missing labels are requested.
        translate_corpus([story], provider, ("en", "de", "nl"))
        self.assertEqual(1, len(provider.calls))

    def test_the_translated_name_is_spoken_and_the_figure_is_the_articles_own(self):
        story = _story()
        translate_corpus([story], _Translator(_faithful), ("en", "de", "nl"))
        german = pipeline.narrate([story], {"language": "de", "expertise": "intermediate", "duration": 3}, None)[0]
        self.assertIn("Leitzins der Zentralbank, 3,5 Prozent.", german["text"])
        self.assertEqual(["f1"], german["fact_ids"])
        self.assertEqual(("Zentralbank hebt Leitzins auf 3,5 %", "de"), pipeline.localized_headline(story, "de"))
        english = pipeline.narrate([story], {"language": "en", "expertise": "intermediate", "duration": 3}, None)[0]
        self.assertIn("Central bank policy rate, 3.5 percent.", english["text"])
        self.assertEqual((story["title"], None), pipeline.localized_headline(story, "en"))
        summary = prepare_summaries([story])["s1"]
        self.assertEqual(summary["segments"]["en"]["fact_ids"], summary["segments"]["de"]["fact_ids"])

    def test_a_translation_that_changes_a_number_is_not_kept(self):
        story = _story()

        def wrong(texts, targets):
            reply = _faithful(texts, targets)
            reply["de"] = ["Zentralbank hebt Leitzins auf 3,75 %", "Leitzins von 3,5"]
            return reply

        translate_corpus([story], _Translator(wrong), ("en", "de", "nl"))
        self.assertNotIn("de", story.get("headlines", {}))
        self.assertNotIn("de", story["facts"][0].get("measure_translations", {}))
        self.assertIn("nl", story["headlines"])
        self.assertEqual((story["title"], None), pipeline.localized_headline(story, "de"))

    def test_an_unusable_reply_or_a_failure_leaves_the_story_as_it_was(self):
        for reply in ({"de": ["only one"], "nl": "not a list"}, "not a dict", None):
            story = _story()
            translate_corpus([story], _Translator(reply), ("en", "de", "nl"))
            self.assertNotIn("headlines", story)
            self.assertNotIn("measure_translations", story["facts"][0])

        class Failing(_Translator):
            def translate_labels(self, texts, source, targets):
                raise RuntimeError("provider down")

        story = _story()
        self.assertEqual(1, translate_corpus([story], Failing(None), ("en", "de", "nl"))["failed_stories"])
        self.assertNotIn("headlines", story)

    def test_no_request_without_a_live_provider_or_for_a_publishers_own_edition(self):
        story, offline = _story(), _Translator(_faithful)
        offline.live = False
        translate_corpus([story], offline, ("en", "de", "nl"))
        self.assertEqual([], offline.calls)
        story["articles"].append({"article_id": "a2", "title": "Zentralbank erhöht Leitzins auf 3,5 %", "source": "Example Wire",
                                  "url": "https://example.test/a2", "language": "de"})
        provider = _Translator(_faithful)
        translate_corpus([story], provider, ("en", "de", "nl"))
        self.assertNotIn("de", story.get("headlines", {}))    # the German edition's own headline is used
        self.assertEqual("Zentralbank erhöht Leitzins auf 3,5 %", pipeline.localized_headline(story, "de")[0])
        self.assertEqual("Leitzins der Zentralbank", story["facts"][0]["measure_translations"]["de"])


if __name__ == "__main__":
    unittest.main()
