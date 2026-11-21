import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "backend" / "demo"
DATA = Path(os.environ.get("BRIEFING_DATA_DIR", ROOT / ".briefing-data")).resolve()
MODE = os.environ.get("BRIEFING_MODE", "demo").strip().lower() or "demo"
if MODE not in {"demo", "live"}:
    raise ValueError("BRIEFING_MODE must be demo or live")
CORPUS = os.environ.get("BRIEFING_CORPUS", "fixture").strip().lower() or "fixture"
if CORPUS not in {"fixture", "event"}:
    raise ValueError("BRIEFING_CORPUS must be fixture or event")

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
    region: str | None = None
    country: str | None = None
    topics: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)

    @field_validator("publication_date")
    @classmethod
    def normalize_date(cls, value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value):
        if not value:
            return None
        value = value.strip()
        return "Europe" if value.lower() == "europe" else value.upper() if len(value) == 2 else value

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value):
        normalized = value.upper() if value else None
        if normalized and normalized not in COUNTRY_CODES:
            raise ValueError("Country must be an ISO alpha-2 code")
        return normalized


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
    listener_id: UUID | None = None
    language: Language | None = None
    region: str = "all"
    voice_preset: str = "default"

    @field_validator("region")
    @classmethod
    def validate_region(cls, value):
        normalized = "Europe" if value.lower() == "europe" else value.upper() if value not in {"all", "Europe"} else value
        if normalized not in REGIONS:
            raise ValueError("Choose all, Europe, or an ISO alpha-2 country code")
        return normalized

    @model_validator(mode="after")
    def one_profile(self):
        if (self.profile_id is None) == (self.wildcard is None):
            raise ValueError("Choose a saved profile or provide a wildcard profile")
        return self


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1)
    region: str = "all"
    language: Language
    cadence: Literal["daily", "weekly"]
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6)
    timezone: str = Field(min_length=1)
    voice_preset: str = Field(default="default", min_length=1, max_length=64)

    @field_validator("region")
    @classmethod
    def normalize_schedule_region(cls, value):
        normalized = "Europe" if value.lower() == "europe" else value.upper() if value not in {"all", "Europe"} else value
        if normalized not in REGIONS:
            raise ValueError("Choose all, Europe, or a valid ISO alpha-2 country code")
        return normalized

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a valid IANA timezone") from None
        return value

    @model_validator(mode="after")
    def validate_schedule(self):
        if (self.cadence == "daily" and self.weekday is not None) or (self.cadence == "weekly" and self.weekday is None):
            raise ValueError("Daily schedules have no weekday and weekly schedules require one")
        if self.region not in REGIONS:
            raise ValueError("Choose all, Europe, or a configured country code")
        return self


COUNTRY_CODES = set("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split())
REGIONS = COUNTRY_CODES | {"all", "Europe"}

EUROPE_COUNTRIES = {"AD", "AL", "AT", "AX", "BA", "BE", "BG", "BY", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FO", "FR", "GB", "GG", "GI", "GR", "HR", "HU", "IE", "IM", "IS", "IT", "JE", "LI", "LT", "LU", "LV", "MC", "MD", "ME", "MK", "MT", "NL", "NO", "PL", "PT", "RO", "RS", "RU", "SE", "SI", "SJ", "SK", "SM", "UA", "VA", "TR"}


# Two male and three female voices suited to reading news, from this account's ElevenLabs library. All
# bill 1 credit per character on eleven_multilingual_v2 (premade voices, and a library voice at rate 1.0)
# and can speak every language that model supports; ElevenLabs has verified Brian and Lily in both German
# and Dutch, and Daniel, Matilda and Sarah in German. Offered only in live mode; demo mode uses the local
# system voice.
VOICE_CATALOGUE = (
    {"id": "daniel", "label": "Daniel", "description": "British, steady broadcaster", "voice_id": "onwK4e9ZLuTAKqWW03F9"},
    {"id": "brian", "label": "Brian", "description": "American, deep and resonant", "voice_id": "nPczCjzI2devNBz1zQrb"},
    {"id": "lily", "label": "Lily", "description": "British, warm and clear", "voice_id": "pFZP5JQG7iQjIQuC4Bku"},
    {"id": "matilda", "label": "Matilda", "description": "American, knowledgeable and professional", "voice_id": "XrExE9yKIg1WjnnlVkGX"},
    {"id": "sarah", "label": "Sarah", "description": "American, approachable and informative", "voice_id": "Nhs7eitvQWFTQBsf0yiT"},
)

# Languages ElevenLabs has verified for each voice (from the account's voice list, 21 Nov 2026). Every voice
# can speak all languages of eleven_multilingual_v2; in an unverified one the accent may vary. Keyed by
# voice ID so the server's default voice is covered too when it is one of these.
VERIFIED_LANGUAGES = {
    "7jynqzOvnVP89nA7TB0m": ("cs", "de", "el", "en", "es", "fr", "hi", "hr", "hu", "id", "it", "ko", "ms", "nl",
                             "no", "pl", "pt", "ru", "tr", "uk", "vi"),  # Nick
    "onwK4e9ZLuTAKqWW03F9": ("de", "en", "tr"),  # Daniel
    "nPczCjzI2devNBz1zQrb": ("ar", "de", "en", "hi", "nl", "pt", "ro", "sk", "zh"),  # Brian
    "pFZP5JQG7iQjIQuC4Bku": ("cs", "de", "en", "it", "nl", "pl", "zh"),  # Lily
    "XrExE9yKIg1WjnnlVkGX": ("ar", "de", "en", "es", "fr", "it"),  # Matilda
    "Nhs7eitvQWFTQBsf0yiT": ("ar", "de", "en", "es", "hr", "it", "ko", "ms", "no", "pl", "pt", "ro", "ru", "ta",
                             "tr", "uk"),  # Sarah
}


def verified_languages(preset_id):
    """Verified languages of a preset's voice, or None when unknown (no label is shown then)."""
    languages = VERIFIED_LANGUAGES.get(resolve_voice_id(preset_id))
    return list(languages) if languages else None


def configured_voice_presets():
    raw = os.getenv("BRIEFING_VOICE_PRESETS")
    if not raw:
        default = {"id": "default", "label": "Default voice", "description": "The voice set for this server",
                   "voice_id_env": "ELEVENLABS_VOICE_ID"}
        return [default, *(dict(row) for row in VOICE_CATALOGUE)] if MODE == "live" else [default]
    configured = json.loads(raw)
    if not isinstance(configured, dict) or "default" not in configured:
        raise ValueError("BRIEFING_VOICE_PRESETS must be an object containing a default preset")
    return [
        {"id": preset_id, "label": str(value.get("label", preset_id)), "voice_id_env": str(value.get("voice_id_env", "ELEVENLABS_VOICE_ID"))}
        for preset_id, value in configured.items()
        if isinstance(value, dict)
    ]


def resolve_voice_id(preset_id):
    """The ElevenLabs voice for a preset; unknown presets use the default voice."""
    presets = configured_voice_presets()
    preset = next((row for row in presets if row["id"] == preset_id), None) or next(row for row in presets if row["id"] == "default")
    return preset.get("voice_id") or os.getenv(preset.get("voice_id_env", "ELEVENLABS_VOICE_ID"), "") or os.getenv("ELEVENLABS_VOICE_ID", "")


def load_profiles():
    path = Path(os.environ.get("BRIEFING_PROFILES", DEMO / "profiles.json"))
    profiles = [Profile.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8-sig"))]
    if not profiles or len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Configure at least one profile and use unique IDs")
    return profiles


def supported_languages():
    """Output languages the app offers: English, German and Dutch, in demo and live mode alike.

    Demo mode speaks them with an offline voice per language. BRIEFING_MULTILINGUAL=0 turns German and
    Dutch off. It is read on every call, so the setting follows the environment.
    """
    if os.environ.get("BRIEFING_MULTILINGUAL", "").strip().lower() in {"0", "false", "no", "off"}:
        return ["en"]
    return ["en", "de", "nl"]
