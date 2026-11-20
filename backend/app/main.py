import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from threading import Lock, Timer
from typing import Literal
from uuid import UUID, uuid4
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from weakref import WeakValueDictionary

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio import audio_file
from .config import (CORPUS, DEMO, MODE, REGIONS, ROOT, BriefingRequest, ScheduleRequest,
                     configured_voice_presets, load_profiles, supported_languages)
from .importer import read_articles
from .service import (NoRelevantStories, generate_briefing, get_store, iso_utc, list_topics,
                      local_delivery_date, next_occurrence, prepare_corpus, recommendation_topics, render_audio,
                      next_schedule_times, schedule_envelope, scheduled_run_id, usage_report, utc_now,
                      wildcard_profile)
from .storage import RUN_LEASE

store = get_store()
retry_locks = WeakValueDictionary()
retry_locks_guard = Lock()
RETRY_WAIT_SECONDS = 5
generation_lock = Lock()
timers_lock = Lock()
local_timers: dict[str, Timer] = {}


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["up", "down"]


class SchedulerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: str = Field(min_length=1)
    schedule_version: int = Field(ge=1)
    generation_at: str = Field(min_length=20)
    delivery_at: str = Field(min_length=20)

    @field_validator("generation_at", "delivery_at")
    @classmethod
    def validate_utc_timestamp(cls, value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Scheduler timestamps must be UTC ISO datetimes") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("Scheduler timestamps must include a UTC offset")
        return iso_utc(parsed)


def _listener(listener_id: UUID) -> str:
    return str(listener_id)


def _configured_profile(profile_id: str, language: str | None = None) -> dict:
    profile = next((item for item in load_profiles() if item.id == profile_id), None)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    result = profile.model_dump()
    if language:
        result["language"] = language
    if result["language"] not in supported_languages():
        raise HTTPException(422, "This output language has not been enabled")
    return result


def _public_schedule(schedule: dict | None) -> dict | None:
    if not schedule:
        return None
    keys = ("schedule_id", "schedule_version", "profile_id", "region", "language", "cadence",
            "local_time", "weekday", "timezone", "voice_preset", "generation_at", "delivery_at", "dispatch_status")
    return {key: schedule[key] for key in keys}


def _visible_briefing(briefing: dict, now=None) -> bool:
    available_at = briefing.get("available_at")
    if not available_at:
        return True
    return datetime.fromisoformat(available_at.replace("Z", "+00:00")) <= (now or utc_now())


def _stored_briefing(briefing_id: str, listener_id: UUID | None) -> dict:
    result = store.briefing(briefing_id)
    if not result:
        raise HTTPException(404, "Briefing not found")
    if result.get("listener_id") and result["listener_id"] != (str(listener_id) if listener_id else None):
        raise HTTPException(404, "Briefing not found")
    if not _visible_briefing(result):
        raise HTTPException(404, "Briefing not found")
    return result


def _dispatch_envelope(envelope: dict) -> bool:
    url = os.getenv("LIQUID_N8N_WEBHOOK_URL", "").strip()
    secret = os.getenv("LIQUID_SCHEDULER_SECRET", "")
    if not url or not secret or not url.lower().startswith("https://"):
        return False
    request = Request(url, data=json.dumps(envelope).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    request.add_unredirected_header("X-Liquid-Scheduler-Secret", secret)
    for attempt in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                return 200 <= response.status < 300 and response.geturl() == url
        except HTTPError as exc:
            if exc.code not in (408, 429) and not 500 <= exc.code < 600:
                return False
        except (URLError, TimeoutError, OSError):
            pass
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    return False


def _schedule_dispatch(schedule: dict) -> None:
    envelope = schedule_envelope(schedule)
    with timers_lock:
        current = store.schedule_for_id(schedule["schedule_id"])
        if not current or schedule_envelope(current) != envelope:
            return
        previous = local_timers.pop(schedule["schedule_id"], None)
        if previous:
            previous.cancel()
    if os.getenv("LIQUID_N8N_WEBHOOK_URL") and os.getenv("LIQUID_SCHEDULER_SECRET"):
        status = "scheduled" if _dispatch_envelope(envelope) else "dispatch_failed"
        store.update_dispatch_status(schedule["schedule_id"], schedule["schedule_version"], status)
        if status == "dispatch_failed" and MODE == "demo":
            _arm_local_schedule(schedule)
        return
    if MODE == "demo":
        store.update_dispatch_status(schedule["schedule_id"], schedule["schedule_version"], "local")
        _arm_local_schedule(schedule)
    else:
        store.update_dispatch_status(schedule["schedule_id"], schedule["schedule_version"], "not_configured")


def _arm_local_schedule(schedule: dict) -> None:
    """Use a one-shot timer in demo mode; production schedules use n8n Wait nodes."""
    envelope = schedule_envelope(schedule)
    with timers_lock:
        current = store.schedule_for_id(schedule["schedule_id"])
        if not current or schedule_envelope(current) != envelope:
            return
        generation_at = datetime.fromisoformat(envelope["generation_at"])
        run = store.scheduled_run(scheduled_run_id(envelope))
        if run and run["status"] == "running":
            lease_end = datetime.fromisoformat(run["claimed_at"]) + RUN_LEASE
            generation_at = max(generation_at, lease_end)
        delay = max(0.0, (generation_at - utc_now()).total_seconds())
        timer = Timer(delay, _run_local_schedule, args=(envelope,))
        timer.daemon = True
        previous = local_timers.get(schedule["schedule_id"])
        if previous:
            previous.cancel()
        local_timers[schedule["schedule_id"]] = timer
    timer.start()


def _finish_or_fail(envelope: dict, briefing: dict | None = None, status: str = "completed",
                    claim_token: str | None = None) -> dict:
    schedule = store.schedule_for_id(envelope["schedule_id"])
    if not schedule:
        return {"status": "stale", "next": None}
    next_times = next_schedule_times(schedule, envelope["delivery_at"])
    run_id = scheduled_run_id(envelope)
    if status == "failed":
        return store.fail_scheduled_run(schedule, envelope, run_id,
                                        local_delivery_date(schedule, envelope["delivery_at"]),
                                        next_times["generation_at"], next_times["delivery_at"],
                                        claim_token=claim_token)
    return store.finalize_scheduled_run(
        run_id, envelope, status=status,
        next_generation_at=next_times["generation_at"], next_delivery_at=next_times["delivery_at"],
        briefing=briefing, claim_token=claim_token,
        fact_ids=[fact_id for segment in (briefing or {}).get("segments", []) for fact_id in segment.get("fact_ids", [])],
    )


def _execute_scheduled_run(envelope: dict) -> dict:
    run_id = scheduled_run_id(envelope)
    schedule = store.schedule_for_id(envelope["schedule_id"])
    if not schedule:
        return {"status": "stale", "next": None}
    if (schedule["schedule_version"] != envelope["schedule_version"]
            or schedule["generation_at"] != envelope["generation_at"]
            or schedule["delivery_at"] != envelope["delivery_at"]):
        return store.run_result(run_id, envelope)
    generation_at = datetime.fromisoformat(envelope["generation_at"].replace("Z", "+00:00"))
    if generation_at > utc_now():
        raise HTTPException(409, "Scheduled generation has not reached generation_at")
    claim = store.claim_scheduled_run(schedule, envelope, local_delivery_date(schedule, envelope["delivery_at"]), run_id)
    if claim["status"] in {"completed", "skipped", "failed", "stale"}:
        return store.run_result(run_id, envelope)
    if claim["status"] == "running":
        raise HTTPException(409, "This scheduled interval is already running")
    if claim["status"] == "duplicate_day":
        return _finish_or_fail(envelope, status="skipped", claim_token=claim.get("claim_token"))
    try:
        profile = _configured_profile(schedule["profile_id"], schedule["language"])
        listener_id = schedule["listener_id"]
        now = utc_now()
        spoken = store.spoken_fact_ids(listener_id, iso_utc(now - timedelta(days=7)), iso_utc(now))
        feedback = [{"story_id": story_id, "rating": rating} for story_id, rating in store.feedback(listener_id).items()]
        with generation_lock:
            briefing = generate_briefing(
                store, profile, listener_id=listener_id, region=schedule["region"],
                voice_preset=schedule["voice_preset"], spoken_fact_ids=spoken,
                feedback_events=feedback, briefing_id=run_id, available_at=envelope["delivery_at"],
                persist=False, scheduled=True,
            )
        if briefing["audio_status"] != "ready":
            store.mark_run_retryable(run_id, claim["claim_token"])
            raise HTTPException(503, "Audio synthesis failed and may be retried")
        return _finish_or_fail(envelope, briefing, claim_token=claim.get("claim_token"))
    except NoRelevantStories:
        return _finish_or_fail(envelope, status="skipped", claim_token=claim.get("claim_token"))
    except ValueError:
        return _finish_or_fail(envelope, status="failed", claim_token=claim.get("claim_token"))
    except Exception as exc:
        if claim.get("claim_token"):
            store.mark_run_retryable(run_id, claim["claim_token"])
        logging.getLogger(__name__).warning("Scheduled briefing failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Scheduled generation may be retried") from exc


def _run_local_schedule(envelope: dict) -> None:
    try:
        result = _execute_scheduled_run(envelope)
    except HTTPException as exc:
        schedule = store.schedule_for_id(envelope["schedule_id"])
        generation_at = datetime.fromisoformat(envelope["generation_at"].replace("Z", "+00:00"))
        if exc.status_code == 409 and schedule and generation_at > utc_now():
            _arm_local_schedule(schedule)
            return
        logging.getLogger(__name__).warning("Local scheduled run failed (%s)", type(exc).__name__)
        result = _finish_or_fail(envelope, status="failed")
    except Exception as exc:
        logging.getLogger(__name__).warning("Local scheduled run failed (%s)", type(exc).__name__)
        result = _finish_or_fail(envelope, status="failed")
    if result.get("next"):
        schedule = store.schedule_for_id(result["next"]["schedule_id"])
        if schedule and schedule["schedule_version"] == result["next"]["schedule_version"]:
            _arm_local_schedule(schedule)


def _authorized_scheduler(secret: str | None) -> None:
    expected = os.getenv("LIQUID_SCHEDULER_SECRET", "")
    if not expected:
        raise HTTPException(503, "Scheduler authentication is not configured")
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(401, "Invalid scheduler credentials")


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    if CORPUS == "fixture" and MODE == "demo" and not store.stories():
        prepare_corpus(store, read_articles(DEMO / "articles.jsonl"))
    if not store.stories():
        logging.getLogger(__name__).warning(
            "No prepared news for LIQUID_MODE=%s LIQUID_CORPUS=%s; run `python -m backend.app import` first", MODE, CORPUS,
        )
    recommendation_topics(store)
    if MODE == "demo" and not os.getenv("LIQUID_N8N_WEBHOOK_URL"):
        for schedule in store.all_schedules():
            _arm_local_schedule(schedule)
    try:
        yield
    finally:
        with timers_lock:
            for timer in local_timers.values():
                timer.cancel()
            local_timers.clear()


app = FastAPI(title="The Liquid Briefing", lifespan=lifespan)


@app.get("/api/profiles")
def profiles():
    return load_profiles()


@app.get("/api/topics")
def topics():
    return recommendation_topics(store)


@app.get("/api/languages")
def languages():
    return supported_languages()


@app.get("/api/regions")
def regions():
    return ["all", "Europe", *sorted(REGIONS - {"all", "Europe"})]


@app.get("/api/voice-presets")
def voice_presets():
    return [{"id": row["id"], "label": row["label"]} for row in configured_voice_presets()]


@app.get("/api/evidence")
def evidence():
    return store.stories()


@app.get("/api/costs")
def costs():
    return usage_report(store)


@app.post("/api/briefings", status_code=201)
def create_briefing(request: BriefingRequest):
    if request.wildcard:
        profile = wildcard_profile(request.wildcard)
        if not set(request.wildcard.topics).issubset(topics()):
            raise HTTPException(422, "Choose topics from the prepared news")
    else:
        profile = _configured_profile(request.profile_id, request.language)
    if request.language:
        profile["language"] = request.language
    if profile["language"] not in supported_languages():
        raise HTTPException(422, "This output language has not been enabled")
    if request.region not in REGIONS:
        raise HTTPException(422, "Choose all, Europe, or a valid country code")
    voice_ids = {row["id"] for row in configured_voice_presets()}
    if request.voice_preset not in voice_ids:
        raise HTTPException(422, "Choose a configured voice preset")
    try:
        listener_id = str(request.listener_id) if request.listener_id else None
        spoken = []
        feedback = []
        if listener_id:
            now = utc_now()
            spoken = store.spoken_fact_ids(listener_id, iso_utc(now - timedelta(days=7)), iso_utc(now))
            feedback = [{"story_id": story_id, "rating": rating} for story_id, rating in store.feedback(listener_id).items()]
        return generate_briefing(store, profile, listener_id=listener_id, region=request.region,
                                 voice_preset=request.voice_preset, spoken_fact_ids=spoken,
                                 feedback_events=feedback)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).warning("Briefing generation failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Briefing generation failed. Check the backend configuration and try again.") from exc


@app.get("/api/listeners/{listener_id}/schedule")
def get_schedule(listener_id: UUID):
    return {"schedule": _public_schedule(store.schedule(_listener(listener_id)))}


@app.put("/api/listeners/{listener_id}/schedule")
def put_schedule(listener_id: UUID, request: ScheduleRequest):
    if request.language not in supported_languages():
        raise HTTPException(422, "This output language has not been enabled")
    _configured_profile(request.profile_id, request.language)
    voice_ids = {row["id"] for row in configured_voice_presets()}
    if request.voice_preset not in voice_ids:
        raise HTTPException(422, "Choose a configured voice preset")
    config = request.model_dump()
    times = next_occurrence(config)
    saved = store.save_schedule(_listener(listener_id), {**config, **times, "schedule_id": uuid4().hex})
    _schedule_dispatch(saved)
    return {"schedule": _public_schedule(store.schedule(_listener(listener_id)))}


@app.get("/api/listeners/{listener_id}/briefings")
def listener_briefings(listener_id: UUID):
    now = utc_now()
    return [briefing for briefing in store.briefings_for_listener(_listener(listener_id))
            if briefing.get("scheduled") and _visible_briefing(briefing, now)]


@app.get("/api/listeners/{listener_id}/feedback")
def listener_feedback(listener_id: UUID):
    return {"ratings": store.feedback(_listener(listener_id))}


@app.put("/api/listeners/{listener_id}/feedback/{story_id}")
def set_feedback(listener_id: UUID, story_id: str, request: FeedbackRequest):
    if story_id not in {story.get("id") for story in store.stories()}:
        raise HTTPException(404, "Story not found")
    store.set_feedback(_listener(listener_id), story_id, request.rating)
    return {"rating": request.rating}


@app.get("/api/briefings/{briefing_id}")
def briefing(briefing_id: str, listener_id: UUID | None = Query(default=None)):
    return _stored_briefing(briefing_id, listener_id)


def _retry_lock(briefing_id):
    with retry_locks_guard:
        return retry_locks.setdefault(briefing_id, Lock())


@app.post("/api/briefings/{briefing_id}/audio")
def retry_audio(briefing_id: str, listener_id: UUID | None = Query(default=None)):
    _stored_briefing(briefing_id, listener_id)
    retry_lock = _retry_lock(briefing_id)
    if not retry_lock.acquire(timeout=RETRY_WAIT_SECONDS):
        current = _stored_briefing(briefing_id, listener_id)
        if current.get("audio_url") and not audio_file(store, str(current["audio_url"]).split("?", 1)[0]):
            return {**current, "audio_url": None, "audio_status": "pending"}
        return current
    try:
        return render_audio(store, _stored_briefing(briefing_id, listener_id))
    finally:
        retry_lock.release()


@app.post("/api/scheduler/run")
def scheduler_run(envelope: SchedulerEnvelope, x_liquid_scheduler_secret: str | None = Header(default=None, alias="X-Liquid-Scheduler-Secret")):
    _authorized_scheduler(x_liquid_scheduler_secret)
    try:
        return _execute_scheduled_run(envelope.model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("Scheduler run failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Scheduled generation may be retried") from exc


@app.post("/api/scheduler/fail")
def scheduler_fail(envelope: SchedulerEnvelope, x_liquid_scheduler_secret: str | None = Header(default=None, alias="X-Liquid-Scheduler-Secret")):
    _authorized_scheduler(x_liquid_scheduler_secret)
    values = envelope.model_dump()
    schedule = store.schedule_for_id(values["schedule_id"])
    if not schedule:
        return {"status": "stale", "next": None}
    if (schedule["schedule_version"] == values["schedule_version"]
            and schedule["generation_at"] == values["generation_at"]
            and datetime.fromisoformat(values["generation_at"].replace("Z", "+00:00")) > utc_now()):
        raise HTTPException(409, "Scheduled generation has not reached generation_at")
    times = next_schedule_times(schedule, values["delivery_at"])
    result = store.fail_scheduled_run(schedule, values, scheduled_run_id(values),
                                      local_delivery_date(schedule, values["delivery_at"]),
                                      times["generation_at"], times["delivery_at"])
    if result["status"] == "running":
        raise HTTPException(409, "This scheduled interval is still running")
    return result


@app.get("/api/audio/{filename}")
def audio(filename: str, listener_id: UUID | None = Query(default=None)):
    if not re.fullmatch(r"[0-9a-f]{32}\.(mp3|wav)", filename):
        raise HTTPException(404, "Audio not found")
    # Audio is shared by content, so access follows the briefings that reference this file.
    viewer = str(listener_id) if listener_id else None
    if not any(
        item.get("audio_status") == "ready" and (not item.get("listener_id") or item["listener_id"] == viewer)
        and _visible_briefing(item)
        for item in store.briefings_for_audio(filename)
    ):
        raise HTTPException(404, "Audio not found")
    path = store.audio_dir / filename
    if not path.is_file():
        raise HTTPException(404, "Audio not found")
    return FileResponse(path, media_type="audio/mpeg" if path.suffix == ".mp3" else "audio/wav")


frontend = ROOT / "frontend" / "dist"
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
