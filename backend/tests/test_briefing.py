import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app.__main__ import compare
from backend.app.config import DEMO, Wildcard, load_profiles
from backend.app.importer import read_articles
from backend.app.service import generate_briefing, list_topics, prepare_corpus, wildcard_profile
from backend.app.storage import Store


class BriefingTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.store = Store(path / "test.sqlite3", path / "audio")
        self.store.initialize()
        self.articles = read_articles(DEMO / "articles.jsonl")
        self.stories = prepare_corpus(self.store, self.articles)

    def test_fixture_story_groups_and_conflicts(self):
        key = json.loads((DEMO / "answer-key.json").read_text(encoding="utf-8"))
        report = compare(self.stories, key)
        for row in report:
            with self.subTest(story=row["label"]):
                self.assertTrue(row["grouped"])
                self.assertFalse(row["extra_members"])
                self.assertFalse(row["missing_values"])
        conflicts = {fact["article_id"] for story in self.stories for fact in story["facts"]
                     if fact["conflict_status"] == "conflict"}
        self.assertIn("a04-en", conflicts)
        self.assertTrue(all("body" not in row for row in self.store.articles()))

    def test_profiles_reuse_evidence_and_wildcard_produces_a_briefing(self):
        profiles = [profile.model_dump() for profile in load_profiles()[:2]]
        profiles.append(wildcard_profile(Wildcard(
            topics=list_topics(self.stories)[:1], expertise="intermediate", duration=3, language="en",
        )))
        with patch("backend.app.service.create_audio", return_value="/api/audio/test.wav"):
            everyday, markets, wildcard = [generate_briefing(self.store, profile) for profile in profiles]
        self.assertNotEqual(everyday["stories"], markets["stories"])
        facts = {fact["id"]: fact for story in self.stories for fact in story["facts"]}
        for briefing in (everyday, markets, wildcard):
            self.assertTrue(briefing["segments"])
            self.assertEqual(briefing, self.store.briefing(briefing["id"]))
            for segment in briefing["segments"]:
                citations = {source["article_id"] for source in segment["sources"]}
                self.assertTrue(segment["fact_ids"])
                self.assertTrue({facts[fact]["article_id"] for fact in segment["fact_ids"]} <= citations)
        self.assertEqual(
            {story["id"]: story for story in self.stories},
            {story["id"]: story for story in self.store.stories()},
        )
