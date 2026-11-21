import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import main, service
from backend.app.config import DEMO, load_profiles
from backend.app.importer import read_articles
from backend.app.pipeline import rank as baseline_rank
from backend.app.providers import Providers
from backend.app.storage import RUN_LEASE, Store


SCHEDULER_HEADERS = {"X-Scheduler-Secret": "test-scheduler-secret"}
ENVELOPE_FIELDS = ("schedule_id", "schedule_version", "generation_at", "delivery_at")


def as_datetime(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory)
        self.store = Store(path / "test.sqlite3", path / "audio")
        self.store.initialize()
        self.stories = service.prepare_corpus(self.store, read_articles(DEMO / "articles.jsonl"))
        self.enterContext(patch.object(main, "store", self.store))
        self.enterContext(patch.dict(os.environ, {
            "BRIEFING_SCHEDULER_SECRET": SCHEDULER_HEADERS["X-Scheduler-Secret"],
            "BRIEFING_N8N_WEBHOOK_URL": "",
        }))
        self.clock = self.enterContext(patch.object(service, "utc_now"))
        self.enterContext(patch.object(main, "utc_now", self.clock))
        self.arm_local_schedule = main._arm_local_schedule
        self.enterContext(patch.object(main, "_arm_local_schedule"))
        self.clock.return_value = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.listener = str(uuid4())
        self.other_listener = str(uuid4())
        self.settings = dict(
            profile_id=load_profiles()[0].id, region="all", language="en", cadence="daily",
            local_time="12:00", weekday=None, timezone="Europe/Berlin", voice_preset="default",
        )

    def listener_path(self, resource, listener=None):
        return f"/api/listeners/{listener or self.listener}/{resource}"

    def save(self, **changes):
        self.settings.update(changes)
        response = self.client.put(self.listener_path("schedule"), json=self.settings)
        self.assertEqual(response.status_code, 200, response.text)
        schedule = response.json()["schedule"]
        return {field: schedule[field] for field in ENVELOPE_FIELDS}

    def run_interval(self, envelope, expected_status, endpoint="run"):
        response = self.client.post(f"/api/scheduler/{endpoint}", json=envelope, headers=SCHEDULER_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], expected_status)
        return result.get("next")

    def fake_audio(self, briefing, store, *_args, **_kwargs):
        filename = f"{briefing['id']}.wav"
        (store.audio_dir / filename).write_bytes(b"stub audio")
        return f"/api/audio/{filename}"

    def test_nonexistent_and_repeated_wall_clock_times_fire_once(self):
        schedule = {**self.settings, "local_time": "02:30"}
        spring = service.next_occurrence(schedule, datetime(2026, 3, 28, 20, tzinfo=timezone.utc))
        self.assertEqual(as_datetime(spring["delivery_at"]), datetime(2026, 3, 29, 1, tzinfo=timezone.utc))
        fall = service.next_occurrence(schedule, datetime(2026, 10, 24, 20, tzinfo=timezone.utc))
        self.assertEqual(as_datetime(fall["delivery_at"]), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
        next_day = service.next_occurrence(schedule, as_datetime(fall["delivery_at"]))
        self.assertEqual(as_datetime(next_day["delivery_at"]), datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc))

    def test_local_restart_waits_for_an_existing_run_lease(self):
        envelope = self.save()
        schedule = self.store.schedule(self.listener)
        self.clock.return_value = as_datetime(envelope["generation_at"]) + timedelta(minutes=2)
        saved_run = {"status": "running", "claimed_at": envelope["generation_at"]}
        with (patch.object(self.store, "scheduled_run", return_value=saved_run),
              patch.object(main, "Timer") as timer, patch.object(main, "local_timers", {})):
            self.arm_local_schedule(schedule)
        self.assertEqual(timer.call_args.args[0], (RUN_LEASE - timedelta(minutes=2)).total_seconds())
        timer.return_value.start.assert_called_once()

    def test_dispatch_retries_transient_http_and_sends_only_the_envelope(self):
        url = "https://example.test/webhook/briefing-schedule"
        success = Mock()
        success.__enter__ = Mock(return_value=SimpleNamespace(status=200, geturl=lambda: url))
        success.__exit__ = Mock(return_value=False)
        transient = HTTPError(url, 503, "Unavailable", None, None)
        with (patch.dict(os.environ, {"BRIEFING_N8N_WEBHOOK_URL": url}),
              patch.object(main, "urlopen", side_effect=[transient, success]) as send,
              patch.object(main.time, "sleep")):
            envelope = self.save()
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            request = call.args[0]
            self.assertEqual(request.full_url, url)
            self.assertEqual(json.loads(request.data), envelope)
            self.assertEqual(request.get_header("X-scheduler-secret"), SCHEDULER_HEADERS["X-Scheduler-Secret"])

    def test_delivery_visibility_idempotency_and_local_time_across_dst(self):
        envelope = self.save()
        generation = as_datetime(envelope["generation_at"])
        delivery = as_datetime(envelope["delivery_at"])
        self.assertEqual(delivery - generation, timedelta(minutes=30))
        self.assertEqual(delivery, datetime(2026, 10, 24, 10, tzinfo=timezone.utc))
        self.assertIn(self.client.post("/api/scheduler/run", json=envelope).status_code, (401, 403))
        early = self.client.post("/api/scheduler/run", json=envelope, headers=SCHEDULER_HEADERS)
        self.assertGreaterEqual(early.status_code, 400)
        self.clock.return_value = generation
        with patch.object(service, "create_audio", side_effect=self.fake_audio) as synthesize:
            next_envelope = self.run_interval(envelope, "completed")
            self.assertEqual(self.run_interval(envelope, "completed"), next_envelope)
            synthesize.assert_called_once()
            briefing_id = synthesize.call_args.args[0]["id"]
        self.assertEqual(as_datetime(next_envelope["delivery_at"]) - delivery, timedelta(hours=25))
        self.assertEqual(self.client.get(self.listener_path("briefings")).json(), [])
        params = {"listener_id": self.listener}
        audio_path = f"/api/audio/{briefing_id}.wav"
        self.assertEqual(self.client.get(audio_path, params=params).status_code, 404)
        self.assertEqual(self.client.get(f"/api/briefings/{briefing_id}", params=params).status_code, 404)

        self.clock.return_value = delivery
        briefings = self.client.get(self.listener_path("briefings")).json()
        self.assertEqual([briefing["id"] for briefing in briefings], [briefing_id])
        self.assertTrue(all(segment["sources"] for segment in briefings[0]["segments"]))
        self.assertEqual(self.client.get(briefings[0]["audio_url"]).status_code, 200)
        self.assertEqual(self.client.get(audio_path, params={"listener_id": self.other_listener}).status_code, 404)
        self.assertEqual(self.client.get(self.listener_path("briefings", self.other_listener)).json(), [])

    def test_stale_edit_skip_and_terminal_failure_keep_recurrence(self):
        superseded = self.save()
        current = self.save(cadence="weekly", weekday=0, region="JP")
        self.assertEqual(current["schedule_id"], superseded["schedule_id"])
        self.assertGreater(current["schedule_version"], superseded["schedule_version"])
        self.assertIsNone(self.run_interval(superseded, "stale"))
        self.clock.return_value = as_datetime(current["generation_at"])
        with patch.object(service, "create_audio") as synthesize:
            next_envelope = self.run_interval(current, "skipped")
            synthesize.assert_not_called()
        self.assertEqual(
            as_datetime(next_envelope["delivery_at"]) - as_datetime(current["delivery_at"]),
            timedelta(days=7),
        )
        self.clock.return_value = as_datetime(next_envelope["generation_at"])
        following = self.run_interval(next_envelope, "failed", endpoint="fail")
        self.assertEqual(self.run_interval(next_envelope, "failed", endpoint="fail"), following)
        self.assertIsNotNone(following)
        self.assertEqual(self.client.get(self.listener_path("briefings")).json(), [])

    def test_transient_synthesis_failure_can_retry_the_same_interval(self):
        envelope = self.save()
        self.clock.return_value = as_datetime(envelope["generation_at"])
        with patch.object(service, "create_audio", side_effect=TimeoutError):
            response = self.client.post("/api/scheduler/run", json=envelope, headers=SCHEDULER_HEADERS)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.store.briefing_count(), 0)
        with patch.object(service, "create_audio", side_effect=self.fake_audio) as synthesize:
            self.assertIsNotNone(self.run_interval(envelope, "completed"))
            synthesize.assert_called_once()
        self.assertEqual(self.store.briefing_count(), 1)

    def test_edit_after_completion_does_not_synthesize_again_on_the_same_local_date(self):
        envelope = self.save()
        self.clock.return_value = as_datetime(envelope["generation_at"])
        with patch.object(service, "create_audio", side_effect=self.fake_audio) as synthesize:
            self.run_interval(envelope, "completed")
            updated = self.save(local_time="13:00")
            updated_delivery = as_datetime(updated["delivery_at"])
            if updated_delivery.date() == as_datetime(envelope["delivery_at"]).date():
                self.clock.return_value = as_datetime(updated["generation_at"])
                updated_delivery = as_datetime(self.run_interval(updated, "skipped")["delivery_at"])
            synthesize.assert_called_once()
        self.assertGreater(updated_delivery.date(), as_datetime(envelope["delivery_at"]).date())

    def test_edit_during_generation_prevents_old_briefing_publication(self):
        envelope = self.save()
        self.clock.return_value = as_datetime(envelope["generation_at"])
        rendering = Event()
        edited = Event()

        def blocking_audio(*args, **kwargs):
            rendering.set()
            self.assertTrue(edited.wait(10))
            return self.fake_audio(*args, **kwargs)

        with patch.object(service, "create_audio", side_effect=blocking_audio), ThreadPoolExecutor(1) as workers:
            callback = workers.submit(
                self.client.post, "/api/scheduler/run", json=envelope, headers=SCHEDULER_HEADERS,
            )
            try:
                self.assertTrue(rendering.wait(10))
                replacement = self.save(local_time="13:00")
                self.assertGreater(replacement["schedule_version"], envelope["schedule_version"])
            finally:
                edited.set()
            response = callback.result(timeout=10)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "stale", "next": None})
        self.clock.return_value = as_datetime(envelope["delivery_at"])
        self.assertEqual(self.client.get(self.listener_path("briefings")).json(), [])
        self.assertEqual(self.store.briefing_count(), 0)

    def test_latest_feedback_is_scoped_to_listener(self):
        story_id = self.stories[0]["id"]
        for rating in ("up", "down"):
            response = self.client.put(self.listener_path(f"feedback/{story_id}"), json={"rating": rating})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"rating": rating})
        self.assertEqual(self.client.get(self.listener_path("feedback")).json(), {"ratings": {story_id: "down"}})
        self.assertEqual(self.client.get(self.listener_path("feedback", self.other_listener)).json(), {"ratings": {}})

    def test_without_n8n_a_live_server_keeps_the_schedule_itself(self):
        # No webhook is configured: the server arms its own timer in live mode as it does in demo mode.
        with patch.object(main, "MODE", "live"):
            self.save()
        schedule = self.store.schedule(self.listener)
        self.assertEqual(schedule["dispatch_status"], "local")
        main._arm_local_schedule.assert_called_once()
        self.assertEqual(main._arm_local_schedule.call_args.args[0]["schedule_id"], schedule["schedule_id"])
        with patch.object(main, "MODE", "live"), patch.object(main, "Timer") as timer, patch.object(main, "local_timers", {}) as timers:
            self.arm_local_schedule(schedule)
            self.assertIn(schedule["schedule_id"], timers)
        timer.return_value.start.assert_called_once()

    def test_a_wildcard_may_ask_for_twelve_stories_but_not_an_unlisted_length(self):
        wildcard = {"topics": [self.stories[0]["topics"][0]], "expertise": "intermediate", "language": "en"}
        with patch.object(service, "create_audio", side_effect=self.fake_audio):
            longer = self.client.post("/api/briefings", json={"language": "en", "wildcard": {**wildcard, "duration": 12}})
            unlisted = self.client.post("/api/briefings", json={"language": "en", "wildcard": {**wildcard, "duration": 9}})
        self.assertEqual(longer.status_code, 201, longer.text)
        self.assertLessEqual(len(longer.json()["stories"]), 12)
        self.assertEqual(unlisted.status_code, 422)

    def test_explicit_geography_filters_story_groups_independently_of_language(self):
        fixture = self.store.path.parent / "regional.jsonl"
        articles = read_articles(DEMO / "articles.jsonl")
        rate_articles = {"a02-en", "a11-de", "a12-nl"}
        for article in articles:
            if article["article_id"] in rate_articles:
                article["region"] = "Europe"
                article["country"] = "DE"
        fixture.write_text("\n".join(json.dumps(article) for article in articles), encoding="utf-8")
        service.prepare_corpus(self.store, read_articles(fixture))
        imported_by_id = {article["article_id"]: article for article in self.store.articles()}
        self.assertEqual(imported_by_id["a02-en"]["country"], "DE")
        self.assertIsNone(imported_by_id["a09-de"].get("country"))
        envelope = self.save(region="DE")
        self.clock.return_value = as_datetime(envelope["generation_at"])
        with patch.object(service, "create_audio", side_effect=self.fake_audio):
            self.run_interval(envelope, "completed")
        self.clock.return_value = as_datetime(envelope["delivery_at"])
        [briefing] = self.client.get(self.listener_path("briefings")).json()
        self.assertEqual(briefing["language"], "en")
        self.assertEqual(len(briefing["stories"]), 1)
        self.assertEqual(len(briefing["segments"]), 1)
        self.assertTrue({source["article_id"] for source in briefing["segments"][0]["sources"]} <= rate_articles)

    def test_rank_receives_seven_day_history_and_feedback_for_only_one_listener(self):
        story_id = self.stories[0]["id"]
        feedback_path = self.listener_path(f"feedback/{story_id}")
        self.assertEqual(self.client.put(feedback_path, json={"rating": "up"}).status_code, 200)
        ranking_contexts = []

        def rank(stories, profile, *, feedback_events=None, spoken_fact_ids=None, region="all"):
            ranking_contexts.append((feedback_events, set(spoken_fact_ids or ()), region))
            return baseline_rank(stories, profile)

        def generate(listener):
            response = self.client.post("/api/briefings", json={
                "listener_id": listener, "profile_id": self.settings["profile_id"],
            })
            self.assertEqual(response.status_code, 201, response.text)
            return response.json()

        with patch.object(service.pipeline, "rank", rank), patch.object(service, "create_audio", side_effect=self.fake_audio):
            first = generate(self.listener)
            first_facts = {fact for segment in first["segments"] for fact in segment["fact_ids"]}
            self.assertTrue(first_facts)
            self.clock.return_value += timedelta(days=1)
            generate(self.other_listener)
            generate(self.listener)
            self.clock.return_value += timedelta(days=8)
            generate(self.listener)

        self.assertTrue(ranking_contexts[0][0])
        self.assertFalse(ranking_contexts[1][0])
        self.assertFalse(ranking_contexts[1][1])
        self.assertTrue(first_facts <= ranking_contexts[2][1])
        self.assertFalse(ranking_contexts[3][1])
        self.assertTrue(all(context[2] == "all" for context in ranking_contexts))

    def test_voice_presets_and_model_are_resolved_on_the_server(self):
        convert = Mock(side_effect=lambda **_kwargs: iter((b"audio",)))
        provider_module = SimpleNamespace(ElevenLabs=Mock(
            return_value=SimpleNamespace(text_to_speech=SimpleNamespace(convert=convert)),
        ))
        presets = {
            "default": {"label": "Default", "voice_id_env": "ELEVENLABS_VOICE_ID"},
            "calm": {"label": "Calm", "voice_id_env": "TEST_CALM_VOICE"},
        }
        with patch.dict("sys.modules", elevenlabs=provider_module), patch.dict(os.environ, {
            "ALLOW_EXTERNAL_PROVIDER_DATA": "1",
            "ELEVENLABS_API_KEY": "test-provider-key",
            "ELEVENLABS_VOICE_ID": "test-default-voice",
            "TEST_CALM_VOICE": "test-preset-voice",
            "BRIEFING_VOICE_PRESETS": json.dumps(presets),
            "ELEVENLABS_MODEL": "test-speech-model",
        }):
            output = self.store.audio_dir / "test.mp3"
            Providers("live", voice_preset="calm").synthesize("Test", output, "en")
            Providers("live", voice_preset="unknown").synthesize("Test", output, "en")
        self.assertEqual(convert.call_args_list[0].kwargs["voice_id"], "test-preset-voice")
        self.assertEqual(convert.call_args_list[1].kwargs["voice_id"], "test-default-voice")
        self.assertTrue(all(call.kwargs["model_id"] == "test-speech-model" for call in convert.call_args_list))

    def test_successful_manual_audio_retry_records_facts(self):
        with patch.object(service, "create_audio", side_effect=TimeoutError):
            response = self.client.post("/api/briefings", json={
                "listener_id": self.listener, "profile_id": self.settings["profile_id"],
            })
        self.assertEqual(response.status_code, 201, response.text)
        briefing = response.json()
        self.assertEqual(briefing["audio_status"], "unavailable")
        with patch.object(service, "create_audio", side_effect=self.fake_audio):
            response = self.client.post(f"/api/briefings/{briefing['id']}/audio", params={
                "listener_id": self.listener,
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["audio_status"], "ready")
        expected = {fact for segment in briefing["segments"] for fact in segment["fact_ids"]}
        now = service.iso_utc(self.clock.return_value)
        self.assertEqual(set(self.store.spoken_fact_ids(self.listener, now, now)), expected)

    # A schedule delivers a saved profile or the listener's own selection of topics, depth and length.
    def custom_schedule(self, **changes):
        topics = self.client.get("/api/topics").json()[:2]
        selection = {"topics": topics, "expertise": "advanced", "duration": 5, "language": "en", **changes}
        return {**{key: value for key, value in self.settings.items() if key != "profile_id"}, "wildcard": selection}

    def test_a_custom_schedule_saves_its_selection_and_delivers_it(self):
        settings = self.custom_schedule()
        response = self.client.put(self.listener_path("schedule"), json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        schedule = response.json()["schedule"]
        self.assertEqual(schedule["profile_id"], "")
        self.assertEqual(schedule["wildcard"], settings["wildcard"])
        self.assertEqual(self.client.get(self.listener_path("schedule")).json()["schedule"]["wildcard"], settings["wildcard"])
        envelope = {field: schedule[field] for field in ENVELOPE_FIELDS}
        self.clock.return_value = as_datetime(envelope["generation_at"])
        with patch.object(service, "create_audio", side_effect=self.fake_audio):
            self.run_interval(envelope, "completed")
        self.clock.return_value = as_datetime(envelope["delivery_at"])
        briefing, = self.client.get(self.listener_path("briefings")).json()
        self.assertEqual(briefing["profile_name"], "Wildcard listener")
        self.assertTrue(briefing["segments"])

    def test_changing_the_selection_is_a_new_version_and_the_schedule_language_is_the_one_spoken(self):
        first = self.client.put(self.listener_path("schedule"), json=self.custom_schedule()).json()["schedule"]
        changed = self.custom_schedule(duration=8, language="en")
        changed["language"] = "de"
        second = self.client.put(self.listener_path("schedule"), json=changed)
        self.assertEqual(second.status_code, 200, second.text)
        second = second.json()["schedule"]
        self.assertEqual(second["schedule_version"], first["schedule_version"] + 1)
        self.assertEqual(second["wildcard"]["duration"], 8)
        self.assertEqual(second["wildcard"]["language"], "de")
        self.assertEqual(main._schedule_profile(self.store.schedule(self.listener))["language"], "de")

    def test_a_schedule_needs_exactly_one_of_a_profile_and_a_selection(self):
        both = {**self.settings, "wildcard": self.custom_schedule()["wildcard"]}
        neither = {key: value for key, value in self.settings.items() if key != "profile_id"}
        unknown = self.custom_schedule(topics=["a topic nobody prepared"])
        for settings in (both, neither, unknown):
            with self.subTest(settings=sorted(settings)):
                self.assertEqual(self.client.put(self.listener_path("schedule"), json=settings).status_code, 422)
        self.assertIsNone(self.client.get(self.listener_path("schedule")).json()["schedule"])

    def test_a_profile_schedule_still_works_and_a_database_from_before_gains_the_column(self):
        envelope = self.save()
        schedule = self.client.get(self.listener_path("schedule")).json()["schedule"]
        self.assertEqual(schedule["profile_id"], self.settings["profile_id"])
        self.assertIsNone(schedule["wildcard"])
        self.assertEqual(main._schedule_profile(self.store.schedule(self.listener))["id"], self.settings["profile_id"])
        with self.store._connect() as db:
            db.execute("ALTER TABLE schedules DROP COLUMN wildcard")
        self.store.initialize()
        self.assertIsNone(self.store.schedule(self.listener)["wildcard"])
        self.assertEqual(envelope["schedule_id"], self.store.schedule(self.listener)["schedule_id"])

    # The local timer calls _run_local_schedule with the envelope it was armed with.
    def armed_schedules(self):
        return [call.args[0] for call in main._arm_local_schedule.call_args_list]

    def test_local_timer_completes_the_run_and_arms_the_next_occurrence(self):
        envelope = self.save()
        self.clock.return_value = as_datetime(envelope["generation_at"])
        main._arm_local_schedule.reset_mock()
        with patch.object(service, "create_audio", side_effect=self.fake_audio) as synthesize:
            main._run_local_schedule(envelope)
            synthesize.assert_called_once()
        self.assertEqual(self.store.briefing_count(), 1)
        self.assertEqual(self.store.scheduled_run(main.scheduled_run_id(envelope))["status"], "completed")
        [armed] = self.armed_schedules()
        self.assertEqual(armed["schedule_id"], envelope["schedule_id"])
        self.assertEqual(armed["schedule_version"], envelope["schedule_version"])
        self.assertGreater(as_datetime(armed["delivery_at"]), as_datetime(envelope["delivery_at"]))

    def test_local_timer_firing_early_re_arms_the_same_schedule_without_a_failure(self):
        envelope = self.save()
        main._arm_local_schedule.reset_mock()
        with patch.object(main, "_finish_or_fail") as finish:
            main._run_local_schedule(envelope)
        finish.assert_not_called()
        [armed] = self.armed_schedules()
        self.assertEqual({field: armed[field] for field in ENVELOPE_FIELDS}, envelope)
        self.assertIsNone(self.store.scheduled_run(main.scheduled_run_id(envelope)))
        self.assertEqual(self.store.briefing_count(), 0)

    def test_local_timer_records_an_unexpected_error_and_keeps_the_recurrence(self):
        envelope = self.save()
        self.clock.return_value = as_datetime(envelope["generation_at"])
        main._arm_local_schedule.reset_mock()
        with patch.object(main, "generate_briefing", side_effect=RuntimeError("boom")):
            main._run_local_schedule(envelope)
        self.assertEqual(self.store.scheduled_run(main.scheduled_run_id(envelope))["status"], "failed")
        self.assertEqual(self.store.briefing_count(), 0)
        [armed] = self.armed_schedules()
        self.assertEqual(armed["schedule_version"], envelope["schedule_version"])
        self.assertGreater(as_datetime(armed["delivery_at"]), as_datetime(envelope["delivery_at"]))

    def test_local_timer_for_an_old_version_publishes_and_re_arms_nothing(self):
        superseded = self.save()
        self.save(local_time="13:00")
        self.clock.return_value = as_datetime(superseded["generation_at"])
        main._arm_local_schedule.reset_mock()
        with patch.object(service, "create_audio", side_effect=self.fake_audio) as synthesize:
            main._run_local_schedule(superseded)
            synthesize.assert_not_called()
        self.assertEqual(self.store.briefing_count(), 0)
        main._arm_local_schedule.assert_not_called()

    def test_startup_arms_every_saved_schedule_when_n8n_is_not_configured(self):
        self.save()
        other = self.client.put(self.listener_path("schedule", self.other_listener), json=self.settings)
        self.assertEqual(other.status_code, 200, other.text)
        main._arm_local_schedule.reset_mock()
        with TestClient(main.app):
            pass
        self.assertEqual(
            {schedule["listener_id"] for schedule in self.armed_schedules()},
            {self.listener, self.other_listener},
        )
        self.assertEqual(main._arm_local_schedule.call_count, 2)

    def test_startup_arms_nothing_when_n8n_owns_the_schedules(self):
        self.save()
        main._arm_local_schedule.reset_mock()
        with patch.dict(os.environ, {"BRIEFING_N8N_WEBHOOK_URL": "https://example.test/webhook/briefing-schedule"}):
            with TestClient(main.app):
                pass
        main._arm_local_schedule.assert_not_called()

    def test_shutdown_cancels_the_local_timers(self):
        timer = Mock()
        with patch.object(main, "local_timers", {"schedule-id": timer}) as timers:
            with TestClient(main.app):
                timer.cancel.assert_not_called()
            timer.cancel.assert_called_once()
            self.assertEqual(timers, {})
