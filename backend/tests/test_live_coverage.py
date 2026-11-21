import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app import pipeline
from backend.app.providers import EMBEDDING_BATCH_TEXTS, Providers

BODY = ("Zorbo said first‑quarter revenue rose to $4.2 billion, up 12% on the year.  Its board met in May. "
        "Orders for the “Atlas” line reached 310 units; margins widened to 18.5 percent as costs fell.")


def article(article_id, body=BODY, language="en", title=None):
    return {"article_id": article_id, "title": title or f"Zorbo results {article_id}", "body": body, "source": "Test Wire",
            "url": f"https://example.invalid/{article_id}", "language": language, "publication_date": "2026-11-19T08:00:00Z"}


def live(reply):
    provider = Mock(spec=Providers, live=True)
    provider.extract.side_effect = reply if callable(reply) else None
    if not callable(reply):
        provider.extract.return_value = reply
    provider.embed.side_effect = RuntimeError("no embeddings in this test")
    return provider


def facts_for(*facts, article_id="a1"):
    return {"stories": [{"title": "Zorbo results a1", "own_title": True, "topics": ["companies"],
                         "facts": [{"article_id": article_id, "value": v, "unit": u, "evidence": e} for v, u, e in facts]}],
            "answered": [article_id]}


class QuoteMatchingTest(unittest.TestCase):
    def extract(self, *facts):
        stories = pipeline.prepare([article("a1")], live(facts_for(*facts)))
        return [fact for story in stories for fact in story["facts"]]

    def test_typographic_variants_and_spacing_still_cite_the_article_text(self):
        facts = self.extract(
            ("12", "%", "first-quarter revenue rose to $4.2 billion, up 12% on the year. Its board met"),
            ("310", "units", 'Orders for the "Atlas" line reached 310 units'),
        )
        self.assertEqual(sorted(f["value"] for f in facts), ["12"])  # "units" is not a known unit, so 310 is not kept
        self.assertIn(facts[0]["evidence"], BODY)

    def test_a_reworded_quote_falls_back_to_the_sentence_that_prints_the_figure(self):
        fact, = self.extract(("18.5", "percent", "Zorbo margins widened to 18.5 percent because its costs fell sharply"))
        self.assertIn(fact["evidence"], BODY)
        self.assertIn("18.5 percent", fact["evidence"])

    def test_a_quote_about_something_else_or_an_absent_figure_is_rejected(self):
        self.assertEqual(self.extract(
            ("12", "%", "12% is also the share of staff who left the firm"),      # a known figure, unrelated wording
            ("19.5", "percent", "margins widened to 19.5 percent as costs fell"),  # figure not in the article
            ("4.2", "billion", "revenue rose to $4.2 billion"),                    # keeps the import on the live path
        )[0]["value"], "4.2")
        self.assertEqual(len(self.extract(("12", "%", "12% is also the share of staff who left the firm"),
                                          ("4.2", "billion", "revenue rose to $4.2 billion"))), 1)

    def test_a_named_currency_is_accepted_and_the_printed_unit_is_kept(self):
        fact, = self.extract(("4.2", "billion dollars", "revenue rose to $4.2 billion, up 12% on the year"))
        self.assertEqual((fact["value"], fact["unit"]), ("4.2", "billion"))
        self.assertEqual(self.extract(("4.2", "million dollars", "revenue rose to $4.2 billion"),
                                      ("12", "%", "up 12% on the year"))[0]["value"], "12")


class ScaleAndBasisPointsTest(unittest.TestCase):
    BODY = ("The trust holds about 800 million shares valued at roughly $135 billion. "
            "A governor called for a 50bps increase in December after a 25 basis points move.")

    def extract(self, *facts):
        reply = {"stories": [{"title": "t", "own_title": True, "topics": [], "facts": [
            {"article_id": "a1", "value": v, "unit": u, "evidence": e} for v, u, e in facts]}], "answered": ["a1"]}
        stories = pipeline.prepare([article("a1", self.BODY)], live(reply))
        return {(fact["value"], fact["unit"]) for story in stories for fact in story["facts"]}

    def test_a_scale_written_into_the_value_is_matched_against_the_printed_scale(self):
        quote = "The trust holds about 800 million shares valued at roughly $135 billion."
        self.assertEqual(self.extract(("800 million", "shares", quote), ("135 billion", "$", quote)),
                         {("800", "million"), ("135", "billion")})
        # A scale the article does not print next to that number is still rejected.
        self.assertEqual(self.extract(("800 billion", "shares", quote), ("135 billion", "$", quote)), {("135", "billion")})

    def test_bps_is_read_as_basis_points(self):
        self.assertEqual(self.extract(("50", "basis points", "A governor called for a 50bps increase in December"),
                                      ("25", "bps", "after a 25 basis points move")),
                         {("50", "basis points"), ("25", "basis points")})


class MeasureNameTest(unittest.TestCase):
    BODY = ("The Freedonia unemployment rate eased from 4.7% to 4.6% across the quarter to September. "
            "Brent crude traded at 92.12 dollars a barrel after the report.")
    GERMAN = "Die Arbeitslosenquote in Freedonia sank von 4,7 % auf 4,6 % im Quartal bis September."

    def story(self, *facts, extra=()):
        reply = {"stories": [{"title": "Labour market", "own_title": True, "topics": ["jobs"], "facts": [
            {"article_id": "a1", "value": v, "unit": u, "measure": m, "evidence": e} for v, u, m, e in facts]}], "answered": ["a1"]}
        articles = [article("a1", self.BODY, title="Freedonia jobless rate falls"), *extra]
        story, = pipeline.prepare(articles, live(reply))
        return story

    def spoken(self, story, language="en"):
        segment, = pipeline.narrate([story], {"language": language, "expertise": "intermediate", "topic_weights": {}}, None)
        return segment

    def test_a_name_built_from_the_sentence_is_kept_and_spoken(self):
        story = self.story(("4.6", "%", "Freedonia unemployment rate", "The Freedonia unemployment rate eased from 4.7% to 4.6%"))
        self.assertEqual(story["facts"][0]["measure"], "Freedonia unemployment rate")
        self.assertIn("Freedonia unemployment rate, 4.6 percent.", self.spoken(story)["text"])
        self.assertNotIn("The report gives", self.spoken(story)["text"])

    def test_a_name_with_words_the_sentence_does_not_say_is_dropped(self):
        for measure in ("Freedonia inflation rate", "stock to buy now", "rate of 4.6", ""):
            with self.subTest(measure=measure):
                story = self.story(("4.6", "%", measure, "The Freedonia unemployment rate eased from 4.7% to 4.6%"))
                self.assertNotIn("measure", story["facts"][0])
        # The figure itself is still verified and spoken, just without a model-given name.
        self.assertIn("4.6 percent", self.spoken(story)["text"])

    def test_a_translated_edition_joins_the_named_claim_instead_of_repeating_it(self):
        german = article("a1-de", self.GERMAN, "de", "Arbeitslosenquote sinkt")
        german["publication_date"] = "2026-11-19T08:02:00Z"

        def reply(batch):
            a = batch[0]
            if a["article_id"] == "a1":
                fact = {"value": "4.6", "unit": "%", "measure": "Freedonia unemployment rate", "evidence": "The Freedonia unemployment rate eased from 4.7% to 4.6%"}
            else:
                fact = {"value": "4,6", "unit": "%", "measure": "Arbeitslosenquote in Freedonia", "evidence": "sank von 4,7 % auf 4,6 %"}
            return {"stories": [{"title": a["title"], "own_title": True, "topics": [], "facts": [dict(fact, article_id=a["article_id"])]}],
                    "answered": [a["article_id"]]}

        english = article("a1", self.BODY + " More detail followed. Wages rose 3 percent. Vacancies fell 2 percent.", title="Freedonia jobless rate falls")
        german["body"] += (" Brent notierte nach dem Bericht bei 92,12 Dollar je Barrel. Die Löhne stiegen um 3 Prozent."
                           " Die offenen Stellen sanken um 2 Prozent.")
        story, = pipeline.prepare([english, german], live(reply))
        self.assertEqual(len(story["articles"]), 2)
        for language, expected in (("en", "Freedonia unemployment rate, 4.6 percent."), ("de", "Arbeitslosenquote in Freedonia, 4,6 Prozent.")):
            segment = self.spoken(story, language)
            self.assertEqual(segment["text"].count("4.6" if language == "en" else "4,6"), 1, segment["text"])
            self.assertIn(expected, segment["text"])
            self.assertEqual(len(segment["fact_ids"]), 2)          # both editions are cited for the one claim
            self.assertEqual(len(segment["sources"]), 2)


class PerLanguageSummaryTest(unittest.TestCase):
    """A figure is named in its article's language, so each language keeps its own citations."""

    def stories(self):
        named_in_english_only = {
            "id": "s1", "title": "Court ruling", "topics": ["policy"], "publication_date": "2026-11-19", "conflicts": [],
            "articles": [{"article_id": "a1", "title": "Court ruling", "source": "Wire", "language": "en", "url": "https://example.invalid/a1"}],
            "facts": [{"id": "f1", "article_id": "a1", "statement": "The licence runs for 10 years.", "evidence": "The licence runs for 10 years.",
                       "value": "10", "unit": "years", "language": "en", "measure": "licence term", "conflict_status": "supported"}],
        }
        both = {
            "id": "s2", "title": "Rate decision", "topics": ["rates"], "publication_date": "2026-11-19", "conflicts": [],
            "articles": [{"article_id": "b1", "title": "Rate decision", "source": "Wire", "language": "en", "url": "https://example.invalid/b1"}],
            "facts": [{"id": "g1", "article_id": "b1", "statement": "The policy rate was held at 3.25 percent.",
                       "evidence": "The policy rate was held at 3.25 percent.", "value": "3.25", "unit": "percent", "language": "en",
                       "measure": "policy rate", "conflict_status": "supported"}],
        }
        return [named_in_english_only, both]

    def test_a_story_speakable_in_one_language_only_is_prepared_and_passed_over_elsewhere(self):
        from backend.app.summaries import prepare_summaries, select_summaries
        stories = self.stories()
        summaries = prepare_summaries(stories)          # used to raise and discard the whole import
        self.assertEqual(set(summaries["s1"]["segments"]), {"en"})
        self.assertEqual(set(summaries["s2"]["segments"]), {"en", "de", "nl"})
        profile = {"language": "en", "expertise": "intermediate", "duration": 3, "topic_weights": {}}
        self.assertEqual([s["id"] for s in select_summaries(stories, profile, summaries)], ["segment-s1", "segment-s2"])
        self.assertEqual([s["id"] for s in select_summaries(stories, dict(profile, language="de"), summaries)], ["segment-s2"])
        self.assertIn("Licence term, 10 years.", summaries["s1"]["variants"][""]["en/intermediate"])


class PerArticleExtractionTest(unittest.TestCase):
    def test_each_article_is_sent_alone_and_stories_take_the_english_headline(self):
        german = article("a1-de", BODY.replace("revenue rose to", "Umsatz stieg auf"), "de", "Zorbo steigert Umsatz")
        english = article("a1", title="Zorbo lifts revenue")
        other = article("b1", "Acme shipped 90 units. Its margin was 7 percent and revenue was 3 million.", title="Acme ships more")
        german["publication_date"] = "2026-11-19T08:02:00Z"

        def reply(batch):
            self.assertEqual(len(batch), 1)
            a = batch[0]
            evidence = "margins widened to 18.5 percent" if a["article_id"] != "b1" else "Its margin was 7 percent"
            value = "18.5" if a["article_id"] != "b1" else "7"
            return {"stories": [{"title": a["title"], "own_title": True, "topics": [],
                                 "facts": [{"article_id": a["article_id"], "value": value, "unit": "percent", "evidence": evidence}]}],
                    "answered": [a["article_id"]]}

        provider = live(reply)
        with patch.dict(os.environ, {"BRIEFING_LIVE_CONCURRENCY": "4"}):
            stories = pipeline.prepare([german, english, other], provider)
        self.assertEqual(provider.extract.call_count, 3)
        by_title = {story["title"]: story for story in stories}
        self.assertEqual(set(by_title), {"Zorbo lifts revenue", "Acme ships more"})
        self.assertEqual({a["article_id"] for a in by_title["Zorbo lifts revenue"]["articles"]}, {"a1", "a1-de"})
        self.assertEqual(len(by_title["Zorbo lifts revenue"]["facts"]), 2)

    def test_an_article_left_out_of_a_reply_is_asked_for_again(self):
        first, second = article("a1"), article("b1", "Acme margin was 7 percent. Revenue was 3 million. Orders were 90 million.", title="Acme")
        calls = []

        def reply(batch):
            calls.append([a["article_id"] for a in batch])
            a = batch[0]
            fact = ("12", "%", "up 12% on the year") if a["article_id"] == "a1" else ("7", "percent", "Acme margin was 7 percent")
            return {"stories": [{"title": a["title"], "own_title": True, "topics": [],
                                 "facts": [{"article_id": a["article_id"], "value": fact[0], "unit": fact[1], "evidence": fact[2]}]}],
                    "answered": [a["article_id"]]}  # answers only the first article of whatever it is given

        with patch.dict(os.environ, {"BRIEFING_LIVE_BATCH_ARTICLES": "24", "BRIEFING_LIVE_CONCURRENCY": "1"}):
            stories = pipeline.prepare([first, second], live(reply))
        self.assertEqual(calls, [["a1", "b1"], ["b1"]])
        self.assertEqual(len(stories), 2)

    def test_a_provider_failure_in_a_parallel_request_aborts_the_import(self):
        def reply(batch):
            if batch[0]["article_id"] == "a3":
                raise ConnectionError("network down")
            return facts_for(("12", "%", "up 12% on the year"), article_id=batch[0]["article_id"])

        provider = live(reply)
        with patch.dict(os.environ, {"BRIEFING_LIVE_CONCURRENCY": "4"}):
            stories = pipeline.prepare([article(f"a{n}", BODY + f" Note {n}.", title=f"Different headline number {n} here") for n in range(1, 6)], provider)
        # Falls back to the local heuristic for the whole corpus (the real provider also marks the failure).
        self.assertTrue(all(story["facts"] for story in stories))
        self.assertEqual(provider.heuristic_fallback_articles, 5)


class ProviderReplyTest(unittest.TestCase):
    def provider(self, client):
        provider = Providers("live")
        return patch.object(Providers, "_client", return_value=client), provider

    def test_per_article_reply_becomes_single_article_stories(self):
        content = json.dumps({"articles": [
            {"article_id": "a1", "topics": ["companies"], "facts": [{"value": "12", "unit": "%", "evidence": "up 12% on the year"}]},
            {"article_id": "zzz", "facts": [{"value": "1", "unit": "%", "evidence": "x"}]},
        ]})
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                                   choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response))))
        patcher, provider = self.provider(client)
        with patcher, patch.dict(os.environ, {"ALLOW_EXTERNAL_PROVIDER_DATA": "1"}):
            reply = provider.extract([article("a1", title="Zorbo lifts revenue")])
        self.assertEqual(reply["answered"], ["a1"])
        story, = reply["stories"]
        self.assertEqual((story["title"], story["facts"][0]["article_id"]), ("Zorbo lifts revenue", "a1"))

    def test_embeddings_are_requested_in_bounded_chunks(self):
        def create(model, input, **options):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=len(input)),
                                   data=[SimpleNamespace(embedding=[float(len(text))]) for text in input])

        embeddings = Mock(side_effect=create)
        client = SimpleNamespace(embeddings=SimpleNamespace(create=embeddings))
        usage = []
        provider = Providers("live", lambda stage, **metrics: usage.append(metrics["input_tokens"]))
        texts = ["x" * (n % 7 + 1) for n in range(EMBEDDING_BATCH_TEXTS * 2 + 10)]
        with patch.object(Providers, "_client", return_value=client):
            vectors = provider.embed(texts, stage="ingestion", dimensions=256)
        self.assertEqual([call.kwargs["input"] for call in embeddings.call_args_list],
                         [texts[:256], texts[256:512], texts[512:]])
        self.assertEqual(vectors, [[float(len(text))] for text in texts])
        self.assertEqual(usage, [256, 256, 10])


if __name__ == "__main__":
    unittest.main()
