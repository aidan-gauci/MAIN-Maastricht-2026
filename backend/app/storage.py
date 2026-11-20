"""Small SQLite persistence layer for the local demo application."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


class Store:
    def __init__(self, path: str | Path, audio_dir: str | Path):
        self.path = Path(path)
        self.audio_dir = Path(audio_dir)

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
                    official INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stories (
                    story_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
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
            """)
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
            rows = db.execute("SELECT article_id,title,source,publication_date,language,url,official FROM articles ORDER BY publication_date,article_id").fetchall()
        return [{**dict(row), "official": bool(row["official"])} for row in rows]

    def stories(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM stories ORDER BY story_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def replace_corpus(self, articles: Iterable[dict[str, Any]], stories: Iterable[dict[str, Any]]) -> None:
        """Replace the corpus as one transaction; article bodies are intentionally discarded."""
        article_rows = []
        for n, item in enumerate(articles):
            article_rows.append((
                self._identifier(item, ("article_id", "id"), f"article-{n}"),
                str(item.get("title", "")), str(item.get("source", "")),
                str(item.get("publication_date", "")), str(item.get("language", "")),
                str(item.get("url", "")), int(bool(item.get("official", False))),
            ))
        story_rows = []
        for n, story in enumerate(stories):
            story_id = self._identifier(story, ("story_id", "id"), f"story-{n}")
            story_rows.append((story_id, json.dumps(story, ensure_ascii=False, separators=(",", ":"))))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM articles")
            db.execute("DELETE FROM stories")
            db.executemany("INSERT INTO articles(article_id,title,source,publication_date,language,url,official) VALUES(?,?,?,?,?,?,?)", article_rows)
            db.executemany("INSERT INTO stories(story_id,payload) VALUES(?,?)", story_rows)

    def save_briefing(self, briefing: dict[str, Any]) -> None:
        briefing_id = self._identifier(briefing, ("briefing_id", "id"), "latest")
        payload = json.dumps(briefing, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute("INSERT INTO briefings(briefing_id,payload,updated_at) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(briefing_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (briefing_id, payload))

    def briefing(self, id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM briefings WHERE briefing_id=?", (str(id),)).fetchone()
        return json.loads(row["payload"]) if row else None

    def briefing_count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM briefings").fetchone()[0]

    def record_usage(self, stage: str, *, mode: str, input_tokens: int = 0, output_tokens: int = 0, characters: int = 0, estimated_usd: float | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO usage(stage,mode,input_tokens,output_tokens,characters,estimated_usd) VALUES(?,?,?,?,?,?)", (stage, mode, input_tokens, output_tokens, characters, estimated_usd))

    def usage(self, mode: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT stage,COUNT(*) calls,COALESCE(SUM(input_tokens),0) input_tokens,
                COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(characters),0) characters,
                CASE WHEN COUNT(estimated_usd)=COUNT(*) THEN SUM(estimated_usd) END estimated_usd
                FROM usage WHERE mode=? GROUP BY stage ORDER BY stage""", (mode,)).fetchall()
        return [dict(row) for row in rows]

    def purge(self) -> None:
        """Remove the database and known generated audio files, without traversing arbitrary trees."""
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        # Audio is app-owned; delete regular files only and leave unexpected subdirectories intact.
        if self.audio_dir.exists() and self.audio_dir.is_dir():
            for candidate in self.audio_dir.iterdir():
                if candidate.is_file() and candidate.suffix.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".tmp"}:
                    candidate.unlink()
