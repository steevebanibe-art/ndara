"""Persistance SQLite. Stdlib uniquement.

Règle de conception : aucune donnée identifiante n'entre ici.
Le numéro de téléphone est haché en amont (models.hash_msisdn) ; le clair
ne quitte jamais la mémoire du processus d'appel.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .models import CorpusItem, Interview, SampleUnit, Turn, utcnow

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    questionnaire_id TEXT NOT NULL,
    language TEXT NOT NULL,
    channel TEXT NOT NULL,
    respondent_hash TEXT NOT NULL,
    stratum TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    disposition TEXT NOT NULL,
    consent_survey TEXT NOT NULL,
    consent_corpus TEXT NOT NULL,
    consent_version TEXT NOT NULL,
    withdrawal_code TEXT,
    cursor INTEGER NOT NULL DEFAULT 0,
    weight REAL,
    quality_score REAL,
    flags TEXT NOT NULL DEFAULT '[]',
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
    interview_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    duration_ms INTEGER,
    raw_text TEXT,
    code TEXT,
    value_num REAL,
    confidence REAL,
    asr_confidence REAL,
    method TEXT NOT NULL,
    relances INTEGER NOT NULL DEFAULT 0,
    audio_path TEXT,
    flags TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (interview_id, seq)
);

CREATE TABLE IF NOT EXISTS sample_units (
    id TEXT PRIMARY KEY,
    msisdn_hash TEXT NOT NULL UNIQUE,
    stratum TEXT NOT NULL,
    country TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    disposition TEXT NOT NULL,
    interview_id TEXT
);

CREATE TABLE IF NOT EXISTS corpus_items (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL,
    respondent_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    step_id TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    transcript TEXT NOT NULL,
    stratum TEXT NOT NULL,
    weight REAL,
    demographics TEXT NOT NULL DEFAULT '{}',
    consent_version TEXT NOT NULL,
    licence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    redactions INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    interview_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_turns_interview ON turns(interview_id);
CREATE INDEX IF NOT EXISTS idx_corpus_interview ON corpus_items(interview_id);
CREATE INDEX IF NOT EXISTS idx_interviews_disp ON interviews(disposition);
"""


class Store:
    def __init__(self, path: str | Path = "data/ndara.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- entretiens ----------

    def save_interview(self, iv: Interview) -> None:
        d = asdict(iv)
        d["flags"] = json.dumps(iv.flags, ensure_ascii=False)
        d["meta"] = json.dumps(iv.meta, ensure_ascii=False)
        cols = ",".join(d.keys())
        placeholders = ",".join(f":{k}" for k in d)
        self.conn.execute(
            f"INSERT INTO interviews ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{k}=excluded.{k}" for k in d if k != "id"),
            d,
        )
        self.conn.commit()

    def get_interview(self, interview_id: str) -> Interview | None:
        row = self.conn.execute(
            "SELECT * FROM interviews WHERE id=?", (interview_id,)
        ).fetchone()
        return _row_to_interview(row) if row else None

    def interviews(self, disposition: str | None = None) -> list[Interview]:
        if disposition:
            rows = self.conn.execute(
                "SELECT * FROM interviews WHERE disposition=?", (disposition,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM interviews").fetchall()
        return [_row_to_interview(r) for r in rows]

    # ---------- tours ----------

    def save_turn(self, t: Turn) -> None:
        d = asdict(t)
        d["flags"] = json.dumps(t.flags, ensure_ascii=False)
        cols = ",".join(d.keys())
        placeholders = ",".join(f":{k}" for k in d)
        self.conn.execute(
            f"INSERT INTO turns ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(interview_id, seq) DO UPDATE SET "
            + ",".join(f"{k}=excluded.{k}" for k in d if k not in ("interview_id", "seq")),
            d,
        )
        self.conn.commit()

    def turns(self, interview_id: str) -> list[Turn]:
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE interview_id=? ORDER BY seq", (interview_id,)
        ).fetchall()
        return [_row_to_turn(r) for r in rows]

    def all_turns(self) -> list[Turn]:
        rows = self.conn.execute("SELECT * FROM turns ORDER BY interview_id, seq").fetchall()
        return [_row_to_turn(r) for r in rows]

    # ---------- base de sondage ----------

    def add_sample_units(self, units: Iterable[SampleUnit]) -> int:
        n = 0
        for u in units:
            try:
                self.conn.execute(
                    "INSERT INTO sample_units (id,msisdn_hash,stratum,country,attempts,"
                    "last_attempt_at,disposition,interview_id) VALUES (?,?,?,?,?,?,?,?)",
                    (u.id, u.msisdn_hash, u.stratum, u.country, u.attempts,
                     u.last_attempt_at, u.disposition, u.interview_id),
                )
                n += 1
            except sqlite3.IntegrityError:
                continue  # numéro déjà dans la base
        self.conn.commit()
        return n

    def sample_units(self) -> list[SampleUnit]:
        rows = self.conn.execute("SELECT * FROM sample_units").fetchall()
        return [SampleUnit(**dict(r)) for r in rows]

    def set_unit_disposition(self, msisdn_hash: str, disposition: str,
                             interview_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE sample_units SET disposition=?, interview_id=COALESCE(?, interview_id), "
            "attempts=attempts+1, last_attempt_at=? WHERE msisdn_hash=?",
            (disposition, interview_id, utcnow(), msisdn_hash),
        )
        self.conn.commit()

    # ---------- corpus ----------

    def save_corpus_item(self, item: CorpusItem) -> None:
        d = asdict(item)
        d["demographics"] = json.dumps(item.demographics, ensure_ascii=False)
        cols = ",".join(d.keys())
        placeholders = ",".join(f":{k}" for k in d)
        self.conn.execute(f"INSERT OR REPLACE INTO corpus_items ({cols}) VALUES ({placeholders})", d)
        self.conn.commit()

    def corpus_items(self, respondent_hash: str | None = None) -> list[dict[str, Any]]:
        if respondent_hash:
            rows = self.conn.execute(
                "SELECT * FROM corpus_items WHERE respondent_hash=?", (respondent_hash,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM corpus_items").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["demographics"] = json.loads(d["demographics"])
            out.append(d)
        return out

    def delete_corpus_for(self, respondent_hash: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM corpus_items WHERE respondent_hash=?", (respondent_hash,)
        )
        self.conn.commit()
        return cur.rowcount

    # ---------- journal ----------

    def log(self, kind: str, interview_id: str | None = None, **payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO events (at, kind, interview_id, payload) VALUES (?,?,?,?)",
            (utcnow(), kind, interview_id, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _row_to_interview(row: sqlite3.Row) -> Interview:
    d = dict(row)
    d["flags"] = json.loads(d.get("flags") or "[]")
    d["meta"] = json.loads(d.get("meta") or "{}")
    return Interview(**d)


def _row_to_turn(row: sqlite3.Row) -> Turn:
    d = dict(row)
    d["flags"] = json.loads(d.get("flags") or "[]")
    return Turn(**d)
