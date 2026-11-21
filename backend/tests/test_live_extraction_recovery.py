"""Live extraction recovers from unusable replies without a real provider call."""
import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app import pipeline, service
from backend.app.providers import ExtractionOutputError, Providers
from backend.app.storage import Store

TOPICS = ("harbour", "turbine", "glacier", "railway")


def make_articles(count=4):
    return [{
        "article_id": f"a{i}", "title": f"{name.title()} authority publishes {name} figures",
        "source": f"{name} Wire", "url": f"https://{name}.example/news", "language": "en",
        "publication_date": "2026-11-19",
        "body": f"The {name} authority reported capacity of {10 * (i + 1)} megawatts.",
    } for i, name in enumerate(TOPICS[:count])]


def provider_story(article):
    return {"title": article["title"], "topics": ["provider"], "facts": [{
        "value": re.search(r"\d+", article["body"]).group(0), "unit": "megawatts",
        "evidence": article["body"], "article_id": article["article_id"],
    }]}


def provider_ids(stories):
    return {article["article_id"] for story in stories if "provider" in story["topics"] for article in story["articles"]}


def heuristic_ids(stories):
    return {article["article_id"] for story in stories if "provider" not in story["topics"] for article in story["articles"]}


class FakeProvider:
    """Mock provider whose extract() answers from the articles it is given."""

    def __init__(self, articles, fail_if=lambda batch: False, error=lambda: ExtractionOutputError("bad reply")):
        self.by_id = {a["article_id"]: a for a in articles}
        self.calls = []
        self.fail_if, self.error = fail_if, error
        self.mock = Mock(live=True, extract=Mock(side_effect=self.extract))

    def extract(self, batch):
        ids = [a["article_id"] for a in batch]
        self.calls.append(ids)
        if self.fail_if(ids):
            raise self.error()
        return {"stories": [provider_story(self.by_id[i]) for i in ids]}


class BatchSplittingTest(unittest.TestCase):
    def setUp(self):
        self.articles = make_articles()
        env = patch.dict(os.environ, {"BRIEFING_LIVE_BATCH_ARTICLES": "24", "BRIEFING_LIVE_BATCH_CHARS": "48000"})
        env.start()
        self.addCleanup(env.stop)

    def test_setup_articles_stay_separate_stories_offline(self):
        self.assertEqual(len(self.articles), len(pipeline._offline_extract(self.articles)))

    def test_invalid_json_batch_is_split_and_both_halves_are_kept(self):
        first_call = {"done": False}

        def fail_once(ids):
            failed, first_call["done"] = not first_call["done"], True
            return failed

        fake = FakeProvider(self.articles, fail_if=fail_once,
                            error=lambda: json.JSONDecodeError("Unterminated string", "{", 1))
        with self.assertLogs("backend.app.pipeline", "WARNING") as logs:
            stories = pipeline.prepare(self.articles, fake.mock)

        self.assertEqual([["a0", "a1", "a2", "a3"], ["a0", "a1"], ["a2", "a3"]], fake.calls)
        self.assertEqual(4, len(stories))
        self.assertEqual({"a0", "a1", "a2", "a3"}, provider_ids(stories))  # no whole-corpus heuristic fallback
        self.assertFalse(heuristic_ids(stories))
        self.assertEqual(0, fake.mock.heuristic_fallback_articles)
        self.assertEqual(1, len(logs.records))
        self.assertIn("4 articles", logs.output[0])
        self.assertNotIn("authority", logs.output[0])  # counts only, never article text

    def test_single_failing_article_alone_uses_the_heuristic(self):
        fake = FakeProvider(self.articles, fail_if=lambda ids: "a2" in ids)
        with self.assertLogs("backend.app.pipeline", "WARNING") as logs:
            stories = pipeline.prepare(self.articles, fake.mock)

        self.assertEqual([["a0", "a1", "a2", "a3"], ["a0", "a1"], ["a2", "a3"], ["a2"], ["a3"]], fake.calls)
        self.assertEqual(4, len(stories))
        self.assertEqual({"a0", "a1", "a3"}, provider_ids(stories))
        self.assertEqual({"a2"}, heuristic_ids(stories))
        self.assertEqual(1, fake.mock.heuristic_fallback_articles)
        heuristic_story = next(story for story in stories if "provider" not in story["topics"])
        self.assertEqual(["30"], [fact["value"] for fact in heuristic_story["facts"]])
        self.assertEqual(["glacier Wire"], [row["source"] for row in heuristic_story["articles"]])
        self.assertEqual(3, len(logs.records))  # two splits and one single-article fallback
        self.assertIn("1 article", logs.output[-1])
        self.assertNotIn("glacier", "\n".join(logs.output).lower())

    def test_every_article_failing_falls_back_for_the_whole_corpus(self):
        fake = FakeProvider(self.articles, fail_if=lambda ids: True)
        with self.assertLogs("backend.app.pipeline", "WARNING"):
            stories = pipeline.prepare(self.articles, fake.mock)
        self.assertEqual(pipeline._offline_extract(self.articles), stories)
        self.assertEqual(len(self.articles), fake.mock.heuristic_fallback_articles)

    def test_provider_failure_still_aborts_the_provider_run_without_splitting(self):
        for error in (ConnectionError("network down"), PermissionError("bad key"), RuntimeError("quota exhausted")):
            with self.subTest(error=type(error).__name__):
                fake = FakeProvider(self.articles, fail_if=lambda ids: True, error=lambda error=error: error)
                stories = pipeline.prepare(self.articles, fake.mock)
                self.assertEqual(pipeline._offline_extract(self.articles), stories)  # as before: caller decides
                self.assertEqual(1, fake.mock.extract.call_count)  # no retry on halves
                self.assertEqual(len(self.articles), fake.mock.heuristic_fallback_articles)

    def test_failure_on_a_later_half_still_aborts_everything(self):
        fake = FakeProvider(self.articles, fail_if=lambda ids: ids == ["a0", "a1", "a2", "a3"],
                            error=lambda: ExtractionOutputError("truncated"))
        original_extract = fake.extract

        def extract(batch):
            if [a["article_id"] for a in batch] == ["a2", "a3"]:
                raise ConnectionError("network down")
            return original_extract(batch)

        fake.mock.extract.side_effect = extract
        with self.assertLogs("backend.app.pipeline", "WARNING"):
            stories = pipeline.prepare(self.articles, fake.mock)
        self.assertEqual(pipeline._offline_extract(self.articles), stories)


def reply(content, finish_reason="stop", prompt_tokens=100):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=7),
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
    )


class RealProviderAdapterTest(unittest.TestCase):
    """Drives Providers._extract with a fake OpenAI client to check usage and failure flags."""

    def setUp(self):
        self.articles = make_articles()
        by_id = {a["article_id"]: a for a in self.articles}
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "test.sqlite3", root / "audio")
        self.store.initialize()
        self.usage = []
        self.providers = Providers("live", lambda stage, **metrics: self.usage.append((stage, metrics)))
        self.by_id = by_id
        self.create = Mock(side_effect=self.answer)
        self.bad_when = lambda ids: False
        self.bad_reply = lambda ids: reply('{"stories": [{"title": "cut off', finish_reason="length")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        for manager in (
            patch.object(Providers, "_client", return_value=client),
            patch.dict(os.environ, {"BRIEFING_LIVE_BATCH_ARTICLES": "24", "BRIEFING_LIVE_BATCH_CHARS": "48000"}),
            patch.object(service, "get_providers", side_effect=lambda store, *, record_usage: self.attach(record_usage)),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    def attach(self, record_usage):
        self.providers.record_usage = record_usage
        return self.providers

    def answer(self, **kwargs):
        ids = [row["article_id"] for row in json.loads(kwargs["messages"][1]["content"])]
        if self.bad_when(ids):
            return self.bad_reply(ids)
        return reply(json.dumps({"stories": [provider_story(self.by_id[i]) for i in ids]}))

    def test_truncated_and_invalid_replies_are_split_and_every_call_is_billed(self):
        replies = {
            "truncated at the token cap": lambda ids: reply('{"stories": [{"title": "cut off', finish_reason="length"),
            "valid-looking but length-capped": lambda ids: reply('{"stories": []}', finish_reason="length"),
            "invalid JSON": lambda ids: reply('{"stories": [{"title": "unterminated'),
            "not an object": lambda ids: reply("[1, 2]"),
        }
        for label, bad_reply in replies.items():
            with self.subTest(label), self.assertLogs("backend.app.pipeline", "WARNING"):
                self.create.reset_mock()
                self.usage.clear()
                self.providers.extraction_failed = False
                self.bad_reply, self.bad_when = bad_reply, lambda ids: len(ids) == 4
                stories = pipeline.prepare(self.articles, self.providers)

                self.assertEqual(3, self.create.call_count)  # whole batch, then two halves
                self.assertEqual(["story_extraction"] * 3, [stage for stage, _ in self.usage])
                self.assertEqual([100] * 3, [metrics["input_tokens"] for _, metrics in self.usage])
                self.assertFalse(self.providers.extraction_failed)
                self.assertEqual({"a0", "a1", "a2", "a3"}, provider_ids(stories))
                self.assertEqual(0, self.providers.heuristic_fallback_articles)

    def test_import_keeps_run_going_and_reports_heuristic_articles(self):
        self.bad_when = lambda ids: "a1" in ids
        report = {}
        # Label translation is a later step with its own requests; this test counts extraction requests.
        with patch.object(service, "translate_corpus", return_value={}):
            stories = service.prepare_corpus(self.store, self.articles, allow_fallback=False, report=report)

        self.assertEqual({"heuristic_fallback_articles": 1}, report)
        self.assertEqual(4, len(stories))
        self.assertEqual(4, len(self.store.stories()))
        self.assertEqual({"a1"}, heuristic_ids(self.store.stories()))
        self.assertFalse(self.providers.extraction_failed)
        self.assertEqual(5, self.create.call_count)  # 4, 2, 2, then 1 and 1
        calls = {row["stage"]: row for row in self.store.usage("live")}["story_extraction"]["calls"]
        self.assertEqual(5, calls)  # failed and retried calls are all billed

    def test_connection_failure_keeps_previous_corpus(self):
        service.prepare_corpus(self.store, self.articles[:1], allow_fallback=False)
        before = self.store.stories()
        self.create.side_effect = ConnectionError("network down")
        self.providers.extraction_failed = False

        with self.assertRaisesRegex(ValueError, "previous corpus was kept"):
            service.prepare_corpus(self.store, self.articles, allow_fallback=False)

        self.assertTrue(self.providers.extraction_failed)
        self.assertEqual(before, self.store.stories())
        self.assertEqual(1, len(self.store.articles()))
        self.create.reset_mock()
        with self.assertRaises(ConnectionError):
            self.providers.extract(self.articles)  # extract() itself re-raises provider failures unchanged
        self.assertEqual(1, self.create.call_count)


if __name__ == "__main__":
    unittest.main()
