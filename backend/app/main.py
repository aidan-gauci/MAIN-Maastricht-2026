import logging
import re
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import CORPUS, DEMO, MODE, ROOT, BriefingRequest, load_profiles, supported_languages
from .importer import read_articles
from .service import generate_briefing, get_store, list_topics, prepare_corpus, render_audio, usage_report, wildcard_profile

store = get_store()
generation_lock = Lock()


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    if CORPUS == "fixture" and MODE == "demo" and not store.stories():
        prepare_corpus(store, read_articles(DEMO / "articles.jsonl"))
    yield


app = FastAPI(title="The Liquid Briefing", lifespan=lifespan)


@app.get("/api/profiles")
def profiles():
    return load_profiles()


@app.get("/api/topics")
def topics():
    return list_topics(store.stories())


@app.get("/api/languages")
def languages():
    return supported_languages()


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
        saved = next((profile for profile in load_profiles() if profile.id == request.profile_id), None)
        if saved is None:
            raise HTTPException(404, "Profile not found")
        profile = saved.model_dump()
    if profile["language"] not in supported_languages():
        raise HTTPException(422, "This output language has not been enabled")
    try:
        with generation_lock:
            return generate_briefing(store, profile)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).warning("Briefing generation failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Briefing generation failed. Check the backend configuration and try again.") from exc


@app.get("/api/briefings/{briefing_id}")
def briefing(briefing_id: str):
    result = store.briefing(briefing_id)
    if result is None:
        raise HTTPException(404, "Briefing not found")
    return result


@app.post("/api/briefings/{briefing_id}/audio")
def retry_audio(briefing_id: str):
    with generation_lock:
        return render_audio(store, briefing(briefing_id))


@app.get("/api/audio/{filename}")
def audio(filename: str):
    if not re.fullmatch(r"[0-9a-f]{32}\.(mp3|wav)", filename):
        raise HTTPException(404, "Audio not found")
    path = store.audio_dir / filename
    if not path.is_file():
        raise HTTPException(404, "Audio not found")
    return FileResponse(path, media_type="audio/mpeg" if path.suffix == ".mp3" else "audio/wav")


frontend = ROOT / "frontend" / "dist"
if frontend.is_dir():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
