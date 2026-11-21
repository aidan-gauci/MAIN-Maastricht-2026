import unittest
from copy import deepcopy
from unittest.mock import DEFAULT, Mock, patch

from backend.app import pipeline
from backend.app.providers import Providers


def fact(article_id, suffix, value="42", evidence=None):
    evidence = evidence or f"The station reported solar capacity of {value} megawatts."
    return {
        "id": f"fact-{article_id}-{suffix}",
        "article_id": article_id,
        "statement": evidence,
        "evidence": evidence,
        "value": value,
        "unit": "megawatts",
        "language": "en",
        "conflict_status": "supported",
    }


def story(story_id, *topics, **overrides):
    article_id = f"article-{story_id}"
    return {
        "id": story_id,
        "title": f"{' '.join(topics).title()} update from {story_id}",
        "topics": list(topics),
        "publication_date": "2026-11-19",
        "articles": [{
            "article_id": article_id,
            "source": "Test Wire",
            "url": f"https://example.invalid/{article_id}",
            "language": "en",
        }],
        "facts": [fact(article_id, "initial")],
        **overrides,
    }


def profile(weights, **overrides):
    return {"id": "shared-profile", "topic_weights": weights, **overrides}


class RecommendationTest(unittest.TestCase):
    def test_batch_matches_individual_rankings_without_provider_calls(self):
        stories = [
            story("prices", "prices", "housing"),
            story("bank", "interest rates", "markets"),
            story("station", "energy", "jobs"),
            story("port", "transport", "trade", publication_date="2026-11-17"),
            story("port-copy", "transport", "trade", title="Transport Trade update from port"),
        ]
        topics = ("inflation", "housing", "rates", "markets", "energy", "employment", "transportation", "trade")
        profiles = [
            profile(
                {topic: ((index + 1) * (position + 3) % 131) / 130 for position, topic in enumerate(topics)},
                id=f"listener-{index}",
                excluded_topics=[topics[index % len(topics)]] if index % 3 == 0 else [],
            )
            for index in range(128)
        ]
        original_stories, original_profiles = deepcopy((stories, profiles))

        with patch.multiple(
            Providers, embed=DEFAULT, extract=DEFAULT, generate_script=DEFAULT, synthesize=DEFAULT,
        ) as methods:
            batch = pipeline.rank_profiles(stories, profiles)
            single = {listener["id"]: pipeline.rank(stories, listener) for listener in profiles}

        for method in methods.values():
            method.assert_not_called()
        self.assertEqual(batch, single)
        self.assertGreater(len({tuple(row["id"] for row in rows) for rows in batch.values()}), 1)
        self.assertEqual(stories, original_stories)
        self.assertEqual(profiles, original_profiles)

    def test_score_weights_use_recency_and_duplicates_across_the_corpus(self):
        european = story("battery-eu", "battery", title="Battery outlook", region="Europe")
        american = story("battery-us", "battery", title="Battery outlook", country_code="US",
                         publication_date="2026-11-20")

        ranked = pipeline.rank([european, american], profile({"battery": 1.0}), region="Europe")

        self.assertEqual([row["id"] for row in ranked], ["battery-eu"])
        # 0.4 topic share + 0.25 strongest matched interest + 0.3 relevance + 0.2 * recency + 0.1 * novelty.
        self.assertEqual(ranked[0]["score_components"], {
            "topic_match": 1.0, "relevance": 1.0, "recency": 0.5, "novelty": 0.5, "score": 1.1,
        })

    def test_a_story_on_one_of_several_chosen_topics_outranks_unrelated_headline_news(self):
        # Three chosen topics: a day-old story on one of them comes before today's biggest unrelated story.
        matching = story("housing-starts", "housing", title="Housing starts rise", publication_date="2026-11-19")
        headline = story("chip-news", "semiconductors", title="Chipmaker results", publication_date="2026-11-20")
        headline["articles"].append({**headline["articles"][0], "article_id": "chip-news-b", "source": "Second Outlet"})
        ranked = pipeline.rank([headline, matching], profile({"housing": 1.0, "jobs": 1.0, "energy": 1.0}))
        self.assertEqual([row["id"] for row in ranked], ["housing-starts", "chip-news"])

    def test_only_latest_rating_changes_weights_for_one_listener(self):
        stories = [story("rated", "energy"), story("other-energy", "energy"), story("forecast", "growth")]
        listener = profile({"energy": 0.49, "growth": 0.51})
        positive = [
            {"story_id": "rated", "rating": "down"},
            {"story_id": "rated", "rating": "up"},
        ]
        original_stories, original_profile, original_events = deepcopy((stories, listener, positive))
        baseline = pipeline.rank(stories, listener)

        liked = pipeline.rank(stories, listener, feedback_events=iter(positive))
        disliked = pipeline.rank(stories, listener, feedback_events=reversed(positive))

        self.assertEqual(self.topic_matches(liked), {"rated": 0.5364, "other-energy": 0.5364, "forecast": 0.4636})
        self.assertEqual(self.topic_matches(disliked), {"rated": 0.4333, "other-energy": 0.4333, "forecast": 0.5667})
        self.assertEqual(baseline[0]["id"], "forecast")
        self.assertEqual(liked[0]["topics"], ["energy"])
        self.assertEqual(pipeline.rank(stories, listener), baseline)
        self.assertEqual(stories, original_stories)
        self.assertEqual(listener, original_profile)
        self.assertEqual(positive, original_events)

    def test_topic_adjustments_accumulate_before_clamping(self):
        stories = [
            story("solar", "energy"), story("wind", "energy"), story("grid", "energy"),
            story("rents", "housing"), story("dwellings", "housing"), story("mortgages", "housing"),
            story("forecast", "growth", "employment"), story("employment", "jobs"),
        ]
        events = [
            {"story_id": story_id, "rating": rating}
            for story_id, rating in (
                ("solar", "up"), ("wind", "up"), ("grid", "down"),
                ("rents", "down"), ("dwellings", "down"), ("mortgages", "up"),
                ("forecast", "up"), ("absent-story", "up"),
            )
        ]

        ranked = pipeline.rank(stories, profile({"energy": 0.95, "housing": 0.05, "jobs": 0.5}),
                               feedback_events=events)

        self.assertEqual(self.topic_matches(ranked), {
            "solar": 0.5882, "wind": 0.5882, "grid": 0.5882, "rents": 0.0, "dwellings": 0.0,
            "mortgages": 0.0, "forecast": 0.4118, "employment": 0.3529,
        })

    def test_trust_checks_all_original_facts_before_applying_memory(self):
        trusted = story("trusted", "rates")
        unsourced = story("unsourced", "rates", facts=[])
        unlinked = story("unlinked", "rates")
        unlinked["articles"][0]["url"] = "file:///article"
        mixed = story("mixed", "rates")
        bad_fact = fact("missing-article", "remembered")
        mixed["facts"].append(bad_fact)

        ranked = pipeline.rank([trusted, unsourced, unlinked, mixed], profile({"rates": 1.0}),
                               spoken_fact_ids=[bad_fact["id"]])

        self.assertEqual([row["id"] for row in ranked], ["trusted"])
        self.assertTrue(ranked[0]["trust_eligible"])

    def test_exclusions_keep_aliases_and_substring_matches_and_reasons_name_topics(self):
        stories = [story("bank", "rates"), story("subsidy", "public housing"), story("station", "energy")]
        listener = profile({"interest rates": 0.8, "energy": 0.2})

        ranked = {row["id"]: row for row in pipeline.rank(stories, listener)}

        self.assertEqual(ranked["bank"]["score_components"]["topic_match"], 0.8)
        self.assertIn("rates", ranked["bank"]["reason"].casefold())
        self.assertIn("energy", ranked["station"]["reason"].casefold())
        filtered = pipeline.rank(stories, dict(listener, excluded_topics=["interest rates", "housing"]))
        self.assertEqual([row["id"] for row in filtered], ["station"])

    def test_region_filters_use_explicit_story_or_article_metadata(self):
        region_tag = story("region", "growth", region="EuRoPe")
        german = story("german", "growth")
        german["articles"][0]["country_code"] = "de"
        british = story("british", "growth", country="GB")
        american = story("american", "growth", country_code="us")
        untranslated = story("language-only", "growth")
        untranslated["articles"][0]["language"] = "nl"
        stories = [region_tag, german, british, american, untranslated]
        listener = profile({"growth": 1.0})

        for region, expected in (
            ("all", {item["id"] for item in stories}),
            ("europe", {"region", "german", "british"}),
            ("DE", {"german"}), ("gb", {"british"}), ("US", {"american"}), ("NL", set()),
        ):
            with self.subTest(region=region):
                self.assertEqual({row["id"] for row in pipeline.rank(stories, listener, region=region)}, expected)

    def test_preparation_preserves_geography_for_local_and_live_extraction(self):
        article = {
            "article_id": "station",
            "title": "Coastal station publishes energy capacity",
            "body": "The station reported solar capacity of 42 megawatts.",
            "source": "Test Wire",
            "url": "https://example.invalid/station",
            "language": "en",
            "publication_date": "2026-11-19",
            "country_code": "NL",
        }
        live = Mock(spec=Providers, live=True)
        live.extract.return_value = {"stories": [{
            "title": article["title"],
            "topics": ["energy"],
            "facts": [fact(article["article_id"], "live", evidence=article["body"])],
        }]}

        for providers in (Providers(), live):
            with self.subTest(live=providers.live):
                prepared = pipeline.prepare([article], providers)
                self.assertEqual(len(prepared), 1)
                self.assertEqual(len(pipeline.rank(prepared, profile({"energy": 1.0}), region="NL")), 1)
                self.assertEqual(pipeline.rank(prepared, profile({"energy": 1.0}), region="DE"), [])
        live.extract.assert_called_once()

    def test_memory_keeps_only_verified_new_facts_for_narration(self):
        original = story("station", "energy")
        old_fact = original["facts"][0]
        remembered = [old_fact["id"]]
        listener = profile({"energy": 1.0}, expertise="beginner", style="Use plain language")
        self.assertEqual(pipeline.rank([original], listener, spoken_fact_ids=remembered), [])
        self.assertEqual(pipeline.rank([original], listener, spoken_fact_ids=[]), pipeline.rank([original], listener))

        updated = deepcopy(original)
        new_fact = fact(old_fact["article_id"], "update", value="70",
                        evidence="The station reported solar capacity of 70 megawatts, up from 42 megawatts.")
        unsupported = fact(old_fact["article_id"], "unsupported", value="91", evidence="Capacity reached 17 megawatts.")
        updated["facts"].extend([new_fact, unsupported])
        ranked = pipeline.rank([updated], listener, spoken_fact_ids=iter(remembered))
        self.assertEqual([(row["id"], row["facts"]) for row in ranked], [(original["id"], [new_fact])])

        live = Mock(spec=Providers, live=True)
        live.generate_script.return_value = "The new solar capacity is 42 megawatts."
        segments = pipeline.narrate(ranked, listener, live)

        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual(segment["fact_ids"], [new_fact["id"]])
        self.assertEqual([source["article_id"] for source in segment["sources"]], [new_fact["article_id"]])
        self.assertIn("70 megawatts", segment["text"])
        self.assertNotIn("42 megawatts", segment["text"])
        live.generate_script.assert_called_once()
        prompt = live.generate_script.call_args.args[0]
        self.assertIn(listener["expertise"], prompt)
        self.assertIn(listener["style"], prompt)
        self.assertNotIn("42 megawatts", prompt)
        self.assertNotIn("17 megawatts", prompt)

        self.assertIn(old_fact["id"], {item["id"] for item in pipeline.rank([updated], listener)[0]["facts"]})
        self.assertEqual(updated["facts"], [old_fact, new_fact, unsupported])

    def test_offline_narration_uses_expertise_for_the_introduction_and_one_claim_wording(self):
        station = story("station", "energy")
        beginner = profile({"energy": 1.0}, expertise="beginner", style="Use plain language")
        advanced = dict(beginner, expertise="advanced")
        figures_first = dict(advanced, style="Lead with the figures")
        ranked = pipeline.rank([station], beginner)

        segments = [pipeline.narrate(ranked, listener, Providers())[0]
                    for listener in (beginner, advanced, figures_first)]

        # Expertise changes the introduction. Whatever the style, a claim is "Label, amount."
        self.assertEqual(segments[1]["text"], segments[2]["text"])
        self.assertNotEqual(segments[0]["text"], segments[1]["text"])
        for listener, segment in zip((beginner, advanced, figures_first), segments):
            intro = pipeline._NARRATION_FRAMES["en"][listener["expertise"]]
            self.assertEqual(segment["text"], f"{intro} Solar capacity, 42 megawatts.")
            self.assertEqual(segment["fact_ids"], [station["facts"][0]["id"]])
            self.assertEqual(segment["sources"], station["articles"])

    def test_live_narration_accepts_reordering_and_rejects_unsupported_claims(self):
        company = story("company", "markets")
        article_id = company["articles"][0]["article_id"]
        company["facts"] = [
            dict(fact(article_id, "revenue", value="70", evidence="The company reported revenue of 70 million."),
                 unit="million"),
            dict(fact(article_id, "profit", value="42", evidence="Operating profit reached 42 million."),
                 unit="million"),
        ]
        listener = profile({"markets": 1.0}, expertise="intermediate")
        ranked = pipeline.rank([company], listener)
        offline = pipeline.narrate(ranked, listener, Providers())[0]
        intro, revenue, profit = offline["text"].removesuffix(".").split(". ")
        self.assertIn("revenue", revenue.casefold())
        self.assertIn("70 million", revenue)
        self.assertIn("profit", profit.casefold())
        self.assertIn("42 million", profit)
        reordered = f"{intro}. {profit}. {revenue}."
        wrong_revenue = revenue.replace("70 million", "42 million")
        wrong_profit = profit.replace("42 million", "70 million")
        drafts = (
            ("reordered claims", reordered, reordered),
            ("wrong measures", f"{intro}. {wrong_revenue}. {wrong_profit}.", offline["text"]),
            ("invented claim", f"{reordered} The company faces financial distress.", offline["text"]),
        )
        live = Mock(spec=Providers, live=True)

        for label, draft, expected in drafts:
            with self.subTest(draft=label):
                live.generate_script.return_value = draft
                self.assertEqual(pipeline.narrate(ranked, listener, live), [dict(offline, text=expected)])
        self.assertEqual(live.generate_script.call_count, len(drafts))

    @staticmethod
    def topic_matches(ranked):
        return {row["id"]: row["score_components"]["topic_match"] for row in ranked}
