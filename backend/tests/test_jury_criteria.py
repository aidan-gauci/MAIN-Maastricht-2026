"""Checks tied to the judging criteria: no investment advice, disclosed voice, importance, fresh stories first."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app import pipeline, service
from backend.app.audio import clip_plan
from backend.app.config import DEMO, load_profiles
from backend.app.importer import read_articles
from backend.app.service import generate_briefing, prepare_corpus
from backend.app.storage import Store


def story(story_id, title, outlets=("Wire",), thread=None, value="3.25"):
    articles = [{"article_id": f"{story_id}-{n}", "title": title, "source": outlet, "language": "en",
                 "url": f"https://example.invalid/{story_id}-{n}"} for n, outlet in enumerate(outlets)]
    evidence = f"The policy rate was held at {value} percent on Tuesday."
    facts = [{"id": f"fact-{story_id}", "article_id": articles[0]["article_id"], "statement": evidence, "evidence": evidence,
              "value": value, "unit": "percent", "language": "en", "conflict_status": "supported"}]
    return {"id": story_id, "title": title, "topics": ["rates"], "publication_date": "2026-11-19", "articles": articles,
            "facts": facts, "conflicts": [], **({"thread_id": thread, "thread_title": title} if thread else {})}


LISTENER = {"name": "Rates", "topic_weights": {"rates": 1.0}, "language": "en", "expertise": "intermediate", "duration": 3}


class NoInvestmentAdviceTest(unittest.TestCase):
    def test_stock_picks_and_buy_or_sell_calls_are_never_selected(self):
        # Invented titles in the shapes the gate must catch. None is a published headline.
        for title in ("Fresh Strong Buy Names for March 3rd", "Momentum Stocks to Buy on March 3rd",
                      "Bull of the Day: Zorbo", "3 Top-Ranked Funds to Lift a Portfolio", "Should You Buy Zorbo Before Results?",
                      "3 Sturdy Funds to Lift Returns as Chip Orders Climb", "4 Water Utility Stocks Likely to Top Estimates",
                      "Should Zorbo Value ETF (ZVL) Sit on Your Radar?", "Is Zorbo Growth (ZGRX) a Strong Fund Pick Today?",
                      "Is Zorbo Equal Weight (ZEW) a Strong ETF Today?", "Zorbo: A Sound Holding Right Now?", "ZRB or QRX: Which Chip Stock Is the Better Bet?",
                      "Zacks Blog Features Zorbo and Quarx", "Buy These 3 Income Funds for Steadier Returns",
                      "Zorbo Stock Slides Before Results: Sell or Hold?", "Zorbo Jumps 65%: Should You Still Buy It?",
                      "Wheat Jumps and Freight Climbs: ETFs in Play", "Can Ether Recover? ETFs to Consider",
                      "Implied Volatility Surging in Zorbo Options", "Options Traders Betting Big on Zorbo",
                      "How to Invest in Copper Miners Today", "A Guide to Solar Power ETFs", "Top Stock Reports: Zorbo and Quarx",
                      "GBP/CHF signal: forecast while the franc rallies", "Zorbo price prediction as momentum fades",
                      "ZRB/QRX has printed a golden cross while the crown slides"):
            with self.subTest(title=title):
                self.assertTrue(pipeline.is_investment_advice({"title": title}))
                self.assertEqual(pipeline._trust_eligible(story("s", title)), (False, "investment advice, not news"))
                self.assertEqual(pipeline.rank([story("s", title)], LISTENER), [])
        for title in ("Central bank holds rate, flags sticky services prices", "Investors buy the dip as yields fall",
                      "Buyout firm to buy rival for 3 billion euros", "Why is Zorbo stock falling on Tuesday?",
                      "Buy now, pay later lender posts first profit", "Zorbo shares fall 5 percent as funds cut exposure",
                      "Zorbo stock jumps 13% as chipmaker raises annual forecast", "Bitcoin ETFs see record inflows",
                      "Jobless claims hit 1971 lows as output surges", "Zorbo (ZRB) Beats Q2 Earnings Estimates"):
            with self.subTest(title=title):
                self.assertFalse(pipeline.is_investment_advice({"title": title}))
                self.assertEqual(len(pipeline.rank([story("s", title)], LISTENER)), 1)

    def test_a_rating_or_price_target_inside_a_news_report_is_never_read_out(self):
        report = story("s", "Zorbo beats quarterly estimates")
        article_id = report["articles"][0]["article_id"]
        for n, (measure, evidence) in enumerate((
                ("Zorbo Zacks Rank", "Zorbo now holds a Zacks Rank of 2 percent."),
                ("analyst price target", "Analysts lifted their price target by 12 percent."),
                ("weekend low", "The primary support level sits 4 percent below the weekend low."),
                ("quarterly revenue growth", "The broker kept its Buy rating after revenue grew 9 percent."))):
            report["facts"].append({"id": f"advice-{n}", "article_id": article_id, "evidence": evidence, "statement": evidence,
                                    "measure": measure, "value": evidence.split()[-2], "unit": "percent", "language": "en",
                                    "conflict_status": "supported"})
        spoken = pipeline.narrate([report], LISTENER, None)[0]
        self.assertEqual(spoken["fact_ids"], ["fact-s"])
        for word in ("Zacks", "target", "9 percent"):
            self.assertNotIn(word, spoken["text"])


class DisclosedVoiceTest(unittest.TestCase):
    def test_every_briefing_opens_by_saying_the_voice_is_synthetic_and_gives_no_advice(self):
        for language, words in (("en", ("AI voice", "not investment advice")), ("de", ("KI-Stimme", "keine Anlageberatung")),
                                ("nl", ("AI-stem", "geen beleggingsadvies"))):
            with self.subTest(language=language):
                segment, = pipeline.narrate([story("s", "Bank holds rate")], dict(LISTENER, language=language), None)
                kind, text, _ = clip_plan({"language": language, "stories": [], "segments": [segment]}, None, "demo")[0]
                self.assertEqual(kind, "intro")
                for word in words:
                    self.assertIn(word, text)


class ImportanceTest(unittest.TestCase):
    def test_wider_coverage_puts_a_story_first_among_equals(self):
        single = story("a", "Bank holds rate", value="3.25")
        covered = story("b", "Regulator holds rate", outlets=("Wire", "Other Wire"), value="4.5")
        developing = story("c", "Ministry holds rate", thread="t", value="5.5")
        follow_up = story("d", "Ministry rate decision draws reaction", thread="t", value="6.5")
        ranked = [s["id"] for s in pipeline.rank([single, developing, follow_up, covered], LISTENER)]
        self.assertEqual(ranked[0], "b")              # two outlets reported it
        self.assertEqual(ranked[-1], "a")             # one outlet, no follow-ups
        self.assertEqual(pipeline._importance(single, 1), 0.0)


class FreshStoriesFirstTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))

    def test_a_returning_listener_hears_unheard_stories_before_any_heard_one(self):
        profile = next(p for p in load_profiles() if p.id == "everyday").model_dump()
        with patch.object(service, "create_audio", return_value="/api/audio/test.wav"):
            first = generate_briefing(self.store, profile, persist=False)
            heard = sorted({fact for segment in first["segments"] for fact in segment["fact_ids"]})
            second = generate_briefing(self.store, profile, spoken_fact_ids=heard, persist=False)
        first_ids, second_ids = [s["id"] for s in first["stories"]], [s["id"] for s in second["stories"]]
        self.assertTrue(second_ids)
        self.assertFalse(set(first_ids) & set(second_ids))   # ten fixture stories: enough unheard ones for a second briefing

    def test_there_are_four_listener_profiles_and_one_is_german(self):
        profiles = load_profiles()
        self.assertEqual(len(profiles), 4)
        self.assertEqual({p.language for p in profiles}, {"en", "de"})


if __name__ == "__main__":
    unittest.main()
