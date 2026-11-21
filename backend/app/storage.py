"""Small SQLite persistence layer for the local demo application."""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable
from uuid import uuid4

from .audio import audio_file

RUN_LEASE = timedelta(hours=1)


class Store:
    def __init__(self, path: str | Path, audio_dir: str | Path):
        self.path = Path(path)
        self.audio_dir = Path(audio_dir)
        self._snapshot_lock = Lock()
        self._snapshot = None

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS articles (
                    article_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    publication_date TEXT NOT NULL,
                    language TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    official INTEGER NOT NULL DEFAULT 0,
                    region TEXT,
                    country TEXT
                );
                CREATE TABLE IF NOT EXISTS stories (
                    story_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS story_summaries (
                    story_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corpus_revision (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    revision TEXT NOT NULL
                );
                INSERT OR IGNORE INTO corpus_revision VALUES (1, 'legacy');
                CREATE TABLE IF NOT EXISTS briefings (
                    briefing_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    characters INTEGER NOT NULL DEFAULT 0,
                    estimated_usd REAL
                );
                CREATE INDEX IF NOT EXISTS usage_mode_stage ON usage(mode, stage);
                CREATE TABLE IF NOT EXISTS schedules (
                    listener_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL UNIQUE,
                    schedule_version INTEGER NOT NULL,
                    profile_id TEXT NOT NULL,
                    region TEXT NOT NULL,
                    language TEXT NOT NULL,
                    cadence TEXT NOT NULL,
                    local_time TEXT NOT NULL,
                    weekday INTEGER,
                    timezone TEXT NOT NULL,
                    voice_preset TEXT NOT NULL,
                    generation_at TEXT NOT NULL,
                    delivery_at TEXT NOT NULL,
                    dispatch_status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    listener_id TEXT NOT NULL,
                    story_id TEXT NOT NULL,
                    rating TEXT NOT NULL CHECK(rating IN ('up','down')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY(listener_id, story_id)
                );
                CREATE TABLE IF NOT EXISTS fact_history (
                    listener_id TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    spoken_at TEXT NOT NULL,
                    briefing_id TEXT NOT NULL,
                    PRIMARY KEY(listener_id, fact_id, briefing_id)
                );
                CREATE INDEX IF NOT EXISTS fact_history_recent ON fact_history(listener_id, spoken_at);
                CREATE TABLE IF NOT EXISTS scheduled_runs (
                    run_id TEXT PRIMARY KEY,
                    listener_id TEXT NOT NULL,
                    schedule_id TEXT NOT NULL,
                    schedule_version INTEGER NOT NULL,
                    generation_at TEXT NOT NULL,
                    delivery_at TEXT NOT NULL,
                    local_delivery_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    briefing_id TEXT,
                    next_envelope TEXT,
                    error TEXT,
                    claim_token TEXT,
                    claimed_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS scheduled_runs_envelope ON scheduled_runs(schedule_id, schedule_version, generation_at);
                CREATE TABLE IF NOT EXISTS run_days (
                    listener_id TEXT NOT NULL,
                    local_delivery_date TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'reserved',
                    PRIMARY KEY(listener_id, local_delivery_date)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(articles)")}
            if "region" not in columns:
                db.execute("ALTER TABLE articles ADD COLUMN region TEXT")
            if "country" not in columns:
                db.execute("ALTER TABLE articles ADD COLUMN country TEXT")
            run_columns = {row["name"] for row in db.execute("PRAGMA table_info(scheduled_runs)")}
            if "claim_token" not in run_columns:
                db.execute("ALTER TABLE scheduled_runs ADD COLUMN claim_token TEXT")
            run_day_columns = {row["name"] for row in db.execute("PRAGMA table_info(run_days)")}
            if "state" not in run_day_columns:
                db.execute("ALTER TABLE run_days ADD COLUMN state TEXT NOT NULL DEFAULT 'finalized'")
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _identifier(item: dict[str, Any], keys: tuple[str, ...], fallback: str) -> str:
        for key in keys:
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value)
        return fallback

    def articles(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT article_id,title,source,publication_date,language,url,official,region,country FROM articles ORDER BY publication_date,article_id").fetchall()
        return [{**dict(row), "official": bool(row["official"])} for row in rows]

    def stories(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM stories ORDER BY story_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def recommendation_snapshot(self, build):
        """Cache a consistent read of the corpus and its summaries per server process."""
        with self._snapshot_lock, self._connect() as db:
            db.execute("BEGIN")
            revision, = db.execute("SELECT revision FROM corpus_revision WHERE id = 1").fetchone()
            if self._snapshot is None or self._snapshot[0] != revision:
                stories = [json.loads(row["payload"]) for row in db.execute(
                    "SELECT payload FROM stories ORDER BY story_id"
                )]
                summaries = {row["story_id"]: json.loads(row["payload"]) for row in db.execute(
                    "SELECT story_id, payload FROM story_summaries"
                )}
                self._snapshot = (revision, build(stories, summaries))
            return self._snapshot[1]

    def replace_corpus(self, articles: Iterable[dict[str, Any]], stories: Iterable[dict[str, Any]],
                       summaries: dict[str, Any] | None = None, *,
                       preparation_usage: Iterable[dict[str, Any]] | None = None) -> None:
        """Publish articles, stories, summaries, and import usage in one transaction.

        Article bodies are discarded. Passing preparation_usage replaces the previous import's
        ingestion and extraction totals while leaving briefing-time usage intact.
        """
        article_rows = []
        for n, item in enumerate(articles):
            article_rows.append((
                self._identifier(item, ("article_id", "id"), f"article-{n}"),
                str(item.get("title", "")), str(item.get("source", "")),
                str(item.get("publication_date", "")), str(item.get("language", "")),
                str(item.get("url", "")), int(bool(item.get("official", False))), item.get("region"), item.get("country"),
            ))
        story_rows = []
        for n, story in enumerate(stories):
            story_id = self._identifier(story, ("story_id", "id"), f"story-{n}")
            story_rows.append((story_id, json.dumps(story, ensure_ascii=False, separators=(",", ":"))))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM articles")
            db.execute("DELETE FROM stories")
            db.execute("DELETE FROM story_summaries")
            db.execute("UPDATE corpus_revision SET revision=? WHERE id=1", (uuid4().hex,))
            db.executemany("INSERT INTO articles(article_id,title,source,publication_date,language,url,official,region,country) VALUES(?,?,?,?,?,?,?,?,?)", article_rows)
            db.executemany("INSERT INTO stories(story_id,payload) VALUES(?,?)", story_rows)
            db.executemany("INSERT INTO story_summaries(story_id,payload) VALUES(?,?)", [
                (story_id, json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
                for story_id, summary in (summaries or {}).items()
            ])
            if preparation_usage is not None:
                db.execute("DELETE FROM usage WHERE stage IN ('ingestion','story_extraction','label_translation')")
                for row in preparation_usage:
                    self._insert_usage(db, **row)

    def save_briefing(self, briefing: dict[str, Any]) -> None:
        briefing_id = self._identifier(briefing, ("briefing_id", "id"), "latest")
        payload = json.dumps(briefing, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute("INSERT INTO briefings(briefing_id,payload,updated_at) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(briefing_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (briefing_id, payload))

    def save_briefing_audio(self, briefing: dict[str, Any]) -> dict[str, Any]:
        """Keep successful audio when concurrent retries overlap."""
        briefing_id = briefing["id"]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM briefings WHERE briefing_id=?", (briefing_id,)).fetchone()
            if row:
                current = json.loads(row["payload"])
                if current.get("audio_status") == "ready" and audio_file(self, current.get("audio_url")):
                    return current
            payload = json.dumps(briefing, ensure_ascii=False, separators=(",", ":"))
            db.execute(
                """INSERT INTO briefings(briefing_id,payload) VALUES(?,?)
                   ON CONFLICT(briefing_id) DO UPDATE SET payload=excluded.payload,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                (briefing_id, payload),
            )
        return briefing

    def briefing(self, id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM briefings WHERE briefing_id=?", (str(id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    @staticmethod
    def _insert_usage(db, stage, *, mode, input_tokens=0, output_tokens=0, characters=0, estimated_usd=None):
        db.execute(
            "INSERT INTO usage(stage,mode,input_tokens,output_tokens,characters,estimated_usd) VALUES(?,?,?,?,?,?)",
            (stage, mode, input_tokens, output_tokens, characters, estimated_usd),
        )
    def briefings_for_audio(self, filename: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            # Callers pass a validated hex filename, so it contains no LIKE wildcards.
            rows = db.execute("SELECT payload FROM briefings WHERE json_extract(payload,'$.audio_url') LIKE ?", ("%/api/audio/" + filename + "%",)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def briefings_for_listener(self, listener_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM briefings WHERE json_extract(payload,'$.listener_id')=? ORDER BY updated_at DESC", (listener_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def schedule_for_id(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
        return dict(row) if row else None

    def all_schedules(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM schedules").fetchall()
        return [dict(row) for row in rows]

    def schedule(self, listener_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM schedules WHERE listener_id=?", (listener_id,)).fetchone()
        return dict(row) if row else None

    def save_schedule(self, listener_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT schedule_id,schedule_version FROM schedules WHERE listener_id=?", (listener_id,)).fetchone()
            values = {**values, "listener_id": listener_id,
                      "schedule_id": old["schedule_id"] if old else values["schedule_id"],
                      "schedule_version": old["schedule_version"] + 1 if old else 1,
                      "dispatch_status": "pending"}
            db.execute("""INSERT INTO schedules(listener_id,schedule_id,schedule_version,profile_id,region,language,cadence,local_time,weekday,timezone,voice_preset,generation_at,delivery_at,dispatch_status)
                VALUES(:listener_id,:schedule_id,:schedule_version,:profile_id,:region,:language,:cadence,:local_time,:weekday,:timezone,:voice_preset,:generation_at,:delivery_at,:dispatch_status)
                ON CONFLICT(listener_id) DO UPDATE SET schedule_id=excluded.schedule_id,schedule_version=excluded.schedule_version,profile_id=excluded.profile_id,region=excluded.region,language=excluded.language,cadence=excluded.cadence,local_time=excluded.local_time,weekday=excluded.weekday,timezone=excluded.timezone,voice_preset=excluded.voice_preset,generation_at=excluded.generation_at,delivery_at=excluded.delivery_at,dispatch_status=excluded.dispatch_status""", values)
            return dict(db.execute("SELECT * FROM schedules WHERE listener_id=?", (listener_id,)).fetchone())

    def update_dispatch_status(self, schedule_id: str, version: int, status: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE schedules SET dispatch_status=? WHERE schedule_id=? AND schedule_version=?", (status, schedule_id, version))

    def set_feedback(self, listener_id: str, story_id: str, rating: str) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO feedback(listener_id,story_id,rating) VALUES(?,?,?)
                ON CONFLICT(listener_id,story_id) DO UPDATE SET rating=excluded.rating,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""", (listener_id, story_id, rating))

    def feedback(self, listener_id: str) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute("SELECT story_id,rating FROM feedback WHERE listener_id=?", (listener_id,)).fetchall()
        return {row["story_id"]: row["rating"] for row in rows}

    def spoken_fact_ids(self, listener_id: str, since: str, until: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT DISTINCT fact_id FROM fact_history WHERE listener_id=? AND spoken_at>=? AND spoken_at<=?", (listener_id, since, until)).fetchall()
        return [row["fact_id"] for row in rows]

    def first_spoken_times(self, listener_id: str, fact_ids: Iterable[str]) -> dict[str, str]:
        """When the listener first heard each fact. A fact recalled later keeps the time it was first heard as news,
        so "the figure heard last time" is the one first heard most recently."""
        wanted = set(fact_ids)
        with self._connect() as db:
            rows = db.execute("SELECT fact_id, MIN(spoken_at) AS first FROM fact_history WHERE listener_id=? GROUP BY fact_id", (listener_id,)).fetchall()
        return {row["fact_id"]: row["first"] for row in rows if row["fact_id"] in wanted}

    def record_spoken_facts(self, listener_id: str, fact_ids: Iterable[str], briefing_id: str, spoken_at: str) -> None:
        with self._connect() as db:
            db.executemany("INSERT OR IGNORE INTO fact_history(listener_id,fact_id,spoken_at,briefing_id) VALUES(?,?,?,?)",
                           [(listener_id, fact_id, spoken_at, briefing_id) for fact_id in set(fact_ids)])

    def claim_scheduled_run(self, schedule: dict[str, Any], envelope: dict[str, str], local_date: str, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM scheduled_runs WHERE run_id=?", (run_id,)).fetchone()
            if run and run["status"] in {"completed", "skipped", "failed", "stale"}:
                next_value = json.loads(run["next_envelope"]) if run["next_envelope"] else None
                current = db.execute("SELECT * FROM schedules WHERE schedule_id=?", (envelope["schedule_id"],)).fetchone()
                if current and current["schedule_id"] == envelope["schedule_id"] and current["schedule_version"] == envelope["schedule_version"] and next_value and next_value["schedule_id"] == current["schedule_id"] and current["generation_at"] == next_value["generation_at"] and current["delivery_at"] == next_value["delivery_at"]:
                    return {"status": run["status"], "next": next_value}
                return {"status": "stale"}
            current = db.execute("SELECT * FROM schedules WHERE schedule_id=?", (envelope["schedule_id"],)).fetchone()
            if not current or current["schedule_version"] != envelope["schedule_version"] or current["generation_at"] != envelope["generation_at"] or current["delivery_at"] != envelope["delivery_at"]:
                return {"status": "stale"}
            if run and run["status"] == "running":
                claimed = datetime.fromisoformat(run["claimed_at"].replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - claimed < RUN_LEASE:
                    return {"status": "running"}
                db.execute("UPDATE scheduled_runs SET status='retryable',error='Previous worker stopped',claimed_at=? WHERE run_id=?", (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), run_id))
            day = db.execute("SELECT run_id,state FROM run_days WHERE listener_id=? AND local_delivery_date=?", (current["listener_id"], local_date)).fetchone()
            if day and day["run_id"] != run_id and day["state"] == "reserved":
                owner = db.execute("SELECT schedule_id,schedule_version,status FROM scheduled_runs WHERE run_id=?", (day["run_id"],)).fetchone()
                if owner and owner["schedule_id"] == current["schedule_id"] and owner["schedule_version"] != current["schedule_version"]:
                    db.execute("UPDATE scheduled_runs SET status='stale',claim_token=NULL,error='Schedule changed before generation completed' WHERE run_id=?", (day["run_id"],))
                    db.execute("DELETE FROM run_days WHERE listener_id=? AND local_delivery_date=? AND run_id=? AND state='reserved'", (current["listener_id"], local_date, day["run_id"]))
                    day = None
                else:
                    return {"status": "running"}
            claim_token = uuid4().hex
            if not run:
                db.execute("INSERT INTO scheduled_runs(run_id,listener_id,schedule_id,schedule_version,generation_at,delivery_at,local_delivery_date,status,claim_token,claimed_at) VALUES(?,?,?,?,?,?,?,'running',?,?)",
                           (run_id, current["listener_id"], current["schedule_id"], current["schedule_version"], envelope["generation_at"], envelope["delivery_at"], local_date, claim_token, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
            else:
                db.execute("UPDATE scheduled_runs SET status='running',claim_token=?,claimed_at=?,error=NULL WHERE run_id=?", (claim_token, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), run_id))
            if day and day["run_id"] != run_id:
                return {"status": "duplicate_day", "claim_token": claim_token}
            if not day:
                db.execute("INSERT INTO run_days(listener_id,local_delivery_date,run_id,state) VALUES(?,?,?,'reserved')", (current["listener_id"], local_date, run_id))
            result = dict(db.execute("SELECT * FROM scheduled_runs WHERE run_id=?", (run_id,)).fetchone())
            result["status"] = "claimed"
            return result

    def mark_run_retryable(self, run_id: str, claim_token: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE scheduled_runs SET status='retryable',claim_token=NULL,error='Transient generation failure' WHERE run_id=? AND status='running' AND claim_token=?", (run_id, claim_token))

    def scheduled_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scheduled_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["next_envelope"] = json.loads(result["next_envelope"]) if result["next_envelope"] else None
        return result

    def run_result(self, run_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        run = self.scheduled_run(run_id)
        if not run:
            return {"status": "stale", "next": None}
        next_envelope = run["next_envelope"]
        current = self.schedule(run["listener_id"])
        if not current or current["schedule_id"] != envelope["schedule_id"] or current["schedule_version"] != envelope["schedule_version"] or not next_envelope or next_envelope["schedule_id"] != current["schedule_id"] or current["generation_at"] != next_envelope["generation_at"] or current["delivery_at"] != next_envelope["delivery_at"]:
            return {"status": "stale", "next": None}
        return {"status": run["status"], "next": next_envelope}

    def finalize_scheduled_run(self, run_id: str, envelope: dict[str, Any], *, status: str,
                               next_generation_at: str, next_delivery_at: str,
                               briefing: dict[str, Any] | None = None,
                               claim_token: str | None = None,
                               fact_ids: Iterable[str] = ()) -> dict[str, Any]:
        next_envelope = {"schedule_id": envelope["schedule_id"], "schedule_version": envelope["schedule_version"],
                         "generation_at": next_generation_at, "delivery_at": next_delivery_at}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM schedules WHERE schedule_id=?", (envelope["schedule_id"],)).fetchone()
            run = db.execute("SELECT * FROM scheduled_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["status"] != "running" or not claim_token or run["claim_token"] != claim_token:
                return {"status": "stale", "next": None}
            if not current or current["schedule_version"] != envelope["schedule_version"] or current["generation_at"] != envelope["generation_at"] or current["delivery_at"] != envelope["delivery_at"]:
                db.execute("UPDATE scheduled_runs SET status='stale',error='Schedule changed during generation' WHERE run_id=? AND status='running'", (run_id,))
                db.execute("UPDATE scheduled_runs SET claim_token=NULL WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM run_days WHERE run_id=? AND state='reserved'", (run_id,))
                return {"status": "stale", "next": None}
            existing = db.execute("SELECT run_id FROM run_days WHERE listener_id=? AND local_delivery_date=?", (current["listener_id"], run["local_delivery_date"])).fetchone()
            if existing and existing["run_id"] != run_id:
                status = "skipped"
                briefing = None
                fact_ids = ()
            else:
                db.execute("UPDATE run_days SET state='finalized' WHERE listener_id=? AND local_delivery_date=? AND run_id=?", (current["listener_id"], run["local_delivery_date"], run_id))
            if briefing:
                briefing_id = self._identifier(briefing, ("briefing_id", "id"), run_id)
                payload = json.dumps(briefing, ensure_ascii=False, separators=(",", ":"))
                db.execute("INSERT INTO briefings(briefing_id,payload,updated_at) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(briefing_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (briefing_id, payload))
                if briefing.get("audio_status") == "ready":
                    db.executemany("INSERT OR IGNORE INTO fact_history(listener_id,fact_id,spoken_at,briefing_id) VALUES(?,?,?,?)",
                                   [(current["listener_id"], fact_id, briefing["available_at"], briefing_id) for fact_id in set(fact_ids)])
            updated = db.execute("UPDATE schedules SET generation_at=?,delivery_at=?,dispatch_status='scheduled' WHERE schedule_id=? AND schedule_version=? AND generation_at=? AND delivery_at=?",
                                 (next_generation_at, next_delivery_at, envelope["schedule_id"], envelope["schedule_version"], envelope["generation_at"], envelope["delivery_at"]))
            if updated.rowcount != 1:
                db.execute("UPDATE scheduled_runs SET status='stale',error='Schedule changed during generation' WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM run_days WHERE run_id=? AND state='reserved'", (run_id,))
                return {"status": "stale", "next": None}
            briefing_id = self._identifier(briefing, ("briefing_id", "id"), run_id) if briefing else None
            db.execute("UPDATE scheduled_runs SET status=?,briefing_id=?,next_envelope=?,error=?,claim_token=NULL WHERE run_id=?",
                       (status, briefing_id, json.dumps(next_envelope), "An interval already ran on this local date" if existing and existing["run_id"] != run_id else None, run_id))
            return {"status": status, "next": next_envelope}

    def fail_scheduled_run(self, schedule: dict[str, Any], envelope: dict[str, Any], run_id: str,
                           local_date: str, next_generation_at: str, next_delivery_at: str,
                           claim_token: str | None = None) -> dict[str, Any]:
        next_envelope = {"schedule_id": envelope["schedule_id"], "schedule_version": envelope["schedule_version"],
                         "generation_at": next_generation_at, "delivery_at": next_delivery_at}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM scheduled_runs WHERE run_id=?", (run_id,)).fetchone()
            if run and run["status"] in {"failed", "completed", "skipped", "stale"}:
                result = dict(run)
                current = db.execute("SELECT * FROM schedules WHERE listener_id=?", (result["listener_id"],)).fetchone()
                next_value = json.loads(result["next_envelope"]) if result["next_envelope"] else None
                if not current or current["schedule_id"] != envelope["schedule_id"] or current["schedule_version"] != envelope["schedule_version"] or not next_value or next_value["schedule_id"] != current["schedule_id"] or current["generation_at"] != next_value["generation_at"] or current["delivery_at"] != next_value["delivery_at"]:
                    return {"status": "stale", "next": None}
                return {"status": result["status"], "next": next_value}
            if run and run["status"] == "running":
                claimed = datetime.fromisoformat(run["claimed_at"].replace("Z", "+00:00"))
                if run["claim_token"] != claim_token and datetime.now(timezone.utc) - claimed < RUN_LEASE:
                    return {"status": "running", "next": None}
            current = db.execute("SELECT * FROM schedules WHERE schedule_id=?", (envelope["schedule_id"],)).fetchone()
            if not current or current["schedule_version"] != envelope["schedule_version"] or current["generation_at"] != envelope["generation_at"] or current["delivery_at"] != envelope["delivery_at"]:
                db.execute("UPDATE scheduled_runs SET status='stale',claim_token=NULL,error='Schedule changed before failure handling' WHERE run_id=? AND status IN ('running','retryable')", (run_id,))
                db.execute("DELETE FROM run_days WHERE run_id=? AND state='reserved'", (run_id,))
                return {"status": "stale", "next": None}
            if not run:
                db.execute("INSERT INTO scheduled_runs(run_id,listener_id,schedule_id,schedule_version,generation_at,delivery_at,local_delivery_date,status,claimed_at) VALUES(?,?,?,?,?,?,?,'failed',?)",
                           (run_id, current["listener_id"], current["schedule_id"], current["schedule_version"], envelope["generation_at"], envelope["delivery_at"], local_date, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
            day = db.execute("SELECT run_id FROM run_days WHERE listener_id=? AND local_delivery_date=?", (current["listener_id"], local_date)).fetchone()
            if not day:
                db.execute("INSERT INTO run_days(listener_id,local_delivery_date,run_id,state) VALUES(?,?,?,'finalized')", (current["listener_id"], local_date, run_id))
            elif day["run_id"] == run_id:
                db.execute("UPDATE run_days SET state='finalized' WHERE run_id=?", (run_id,))
            updated = db.execute("UPDATE schedules SET generation_at=?,delivery_at=?,dispatch_status='scheduled' WHERE schedule_id=? AND schedule_version=? AND generation_at=? AND delivery_at=?",
                                 (next_generation_at, next_delivery_at, envelope["schedule_id"], envelope["schedule_version"], envelope["generation_at"], envelope["delivery_at"]))
            if updated.rowcount != 1:
                db.execute("UPDATE scheduled_runs SET status='stale',error='Schedule changed before failure handling' WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM run_days WHERE run_id=? AND state='reserved'", (run_id,))
                return {"status": "stale", "next": None}
            db.execute("UPDATE scheduled_runs SET status='failed',next_envelope=?,error='Scheduled generation failed',claim_token=NULL WHERE run_id=?", (json.dumps(next_envelope), run_id))
            return {"status": "failed", "next": next_envelope}


    def briefing_count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM briefings").fetchone()[0]

    def record_usage(self, stage: str, **metrics) -> None:
        with self._connect() as db:
            self._insert_usage(db, stage, **metrics)

    @staticmethod
    def _usage(db, mode):
        rows = db.execute("""SELECT stage,COUNT(*) calls,COALESCE(SUM(input_tokens),0) input_tokens,
            COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(characters),0) characters,
            CASE WHEN COUNT(estimated_usd)=COUNT(*) THEN SUM(estimated_usd) END estimated_usd
            FROM usage WHERE mode=? GROUP BY stage ORDER BY stage""", (mode,)).fetchall()
        return [dict(row) for row in rows]

    def usage(self, mode: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return self._usage(db, mode)

    def cost_snapshot(self, mode):
        """Read usage and corpus counts from the same revision."""
        with self._connect() as db:
            db.execute("BEGIN")
            usage = self._usage(db, mode)
            article_count, = db.execute("SELECT COUNT(*) FROM articles").fetchone()
            story_count, = db.execute("SELECT COUNT(*) FROM stories").fetchone()
            briefing_count, narrated_count = db.execute("""
                SELECT COUNT(*),
                       COUNT(CASE WHEN json_extract(payload,'$.audio_status') = 'ready'
                                   AND json_extract(payload,'$.audio_url') IS NOT NULL THEN 1 END)
                FROM briefings
            """).fetchone()
        return usage, article_count, story_count, briefing_count, narrated_count

    def purge(self) -> None:
        """Remove the database and known generated audio files, without traversing arbitrary trees.

        Safe to repeat. Raises ``RuntimeError`` when a running backend still holds the files.
        """
        self._snapshot = None
        try:
            # Audio is app-owned; delete regular files only and leave unexpected subdirectories intact.
            if self.audio_dir.is_dir():
                for candidate in self.audio_dir.iterdir():
                    generated_audio = candidate.suffix.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".tmp"}
                    lock_file = candidate.name.startswith(".audio-") and candidate.suffix == ".lock"
                    manifest = candidate.name.endswith(".tmp.txt")
                    if candidate.is_file() and (generated_audio or lock_file or manifest):
                        candidate.unlink()
            for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"), Path(str(self.path) + "-journal")):
                candidate.unlink(missing_ok=True)
        except PermissionError:
            raise RuntimeError(f"Could not delete files in {self.path.parent}. Stop the backend and run the command again") from None
        # Remove the folders the app created once they are empty; anything else is left in place.
        for directory in (self.audio_dir, self.path.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
