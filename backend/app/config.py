import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "backend" / "demo"
DATA = Path(os.environ.get("LIQUID_DATA_DIR", ROOT / ".liquid-data")).resolve()
MODE = os.environ.get("LIQUID_MODE", "demo").strip().lower() or "demo"
if MODE not in {"demo", "live"}:
    raise ValueError("LIQUID_MODE must be demo or live")
CORPUS = os.environ.get("LIQUID_CORPUS", "fixture").strip().lower() or "fixture"
if CORPUS not in {"fixture", "event"}:
    raise ValueError("LIQUID_CORPUS must be fixture or event")

Expertise = Literal["beginner", "intermediate", "advanced"]
Language = Literal["en", "de", "nl"]
Duration = Literal[3, 5, 8]
STYLES = {
    "beginner": "Explain technical terms and connect the story to everyday decisions",
    "intermediate": "Explain why each story matters and add context",
    "advanced": "Focus on drivers, tradeoffs, and market context",
}


class Article(BaseModel):
    article_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source: str = Field(min_length=1)
    publication_date: datetime
    language: Language
    body: str = Field(min_length=1)
    url: HttpUrl
    official: bool = False
    topics: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)

    @field_validator("publication_date")
    @classmethod
    def normalize_date(cls, value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    topic_weights: dict[str, float]
    excluded_topics: list[str] = Field(default_factory=list)
    expertise: Expertise
    language: Language
    duration: Duration
    style: str

    @field_validator("topic_weights")
    @classmethod
    def validate_weights(cls, value):
        if any(not 0 <= weight <= 1 for weight in value.values()):
            raise ValueError("Topic weights must be between 0 and 1")
        return value


class Wildcard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[str] = Field(min_length=1, max_length=12)
    expertise: Expertise
    language: Language
    duration: Duration


class BriefingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = None
    wildcard: Wildcard | None = None

    @model_validator(mode="after")
    def one_profile(self):
        if (self.profile_id is None) == (self.wildcard is None):
            raise ValueError("Choose a saved profile or provide a wildcard profile")
        return self


def load_profiles():
    path = Path(os.environ.get("LIQUID_PROFILES", DEMO / "profiles.json"))
    profiles = [Profile.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8-sig"))]
    if not profiles or len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Configure at least one profile and use unique IDs")
    return profiles


def supported_languages():
    if MODE == "live" and os.environ.get("LIQUID_MULTILINGUAL") == "1":
        return ["en", "de", "nl"]
    return ["en"]
