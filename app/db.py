"""Database SQLite per il Coach Personale.

Schema:
    activities    — corse, bici, palestra, ecc. con tutti i metric Garmin
    sleep         — punteggio sonno, stadi, durate, HRV notturno
    wellness      — Body Battery, Stress, HRV daily, Recovery Time per giorno
    training      — Training Readiness, Training Status, VO2max, Endurance/Hill score
    chat_messages — messaggi salvati delle chat (coach generale + per attività)
    meta          — coppie chiave/valore per stato dell'app (es. ultima sync)
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

# Path del database — env var DATA_DIR ha precedenza (per il deploy cloud su volume)
_DATA_DIR = os.environ.get("DATA_DIR") or str(Path(__file__).parent / "data")
DB_PATH = Path(_DATA_DIR) / "coach.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id              TEXT PRIMARY KEY,         -- garmin activityId or local id
    source          TEXT NOT NULL,            -- 'garmin' | 'import' | 'manual'
    type            TEXT,                     -- running, cycling, strength_training, ...
    name            TEXT,
    start_time      TEXT NOT NULL,            -- ISO 8601
    duration_sec    REAL,
    moving_sec      REAL,
    distance_m      REAL,
    elev_gain_m     REAL,
    elev_loss_m     REAL,
    avg_pace_sec    REAL,                     -- sec/km
    avg_hr          REAL,
    max_hr          REAL,
    avg_cadence     REAL,
    max_cadence     REAL,
    avg_power       REAL,
    max_power       REAL,
    avg_speed       REAL,                     -- m/s
    max_speed       REAL,
    calories        REAL,
    aerobic_te      REAL,                     -- training effect aerobic
    anaerobic_te    REAL,
    training_load   REAL,
    vo2max          REAL,
    pte             REAL,                     -- performance condition
    summary_json    TEXT,                     -- raw Garmin summary blob
    detail_json     TEXT,                     -- splits, laps, samples (lazy-loaded)
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time DESC);
CREATE INDEX IF NOT EXISTS idx_activities_type  ON activities(type);

CREATE TABLE IF NOT EXISTS sleep (
    date            TEXT PRIMARY KEY,         -- YYYY-MM-DD (calendar date of the night ending)
    score           REAL,
    duration_sec    REAL,
    deep_sec        REAL,
    light_sec       REAL,
    rem_sec         REAL,
    awake_sec       REAL,
    avg_hrv         REAL,                     -- ms
    avg_resp_rate   REAL,
    avg_spo2        REAL,
    avg_stress      REAL,
    raw_json        TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wellness (
    date            TEXT PRIMARY KEY,         -- YYYY-MM-DD
    body_battery_high     REAL,
    body_battery_low      REAL,
    body_battery_charged  REAL,
    body_battery_drained  REAL,
    stress_avg            REAL,
    stress_max            REAL,
    hrv_status            TEXT,               -- balanced/unbalanced/low/poor
    hrv_weekly_avg        REAL,
    hrv_last_night        REAL,
    resting_hr            REAL,
    steps                 INTEGER,
    floors_climbed        REAL,
    active_kcal           REAL,
    raw_json              TEXT,
    created_at            TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS training (
    date                  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    training_readiness    REAL,               -- 0-100
    training_status       TEXT,               -- productive, recovery, peaking, ...
    acute_load            REAL,
    load_focus            TEXT,
    recovery_time_hours   REAL,
    vo2max_running        REAL,
    vo2max_cycling        REAL,
    endurance_score       REAL,
    hill_score            REAL,
    race_predictor_5k     REAL,
    race_predictor_10k    REAL,
    race_predictor_half   REAL,
    race_predictor_full   REAL,
    raw_json              TEXT,
    created_at            TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL,            -- 'general' or 'activity:<activityId>'
    role            TEXT NOT NULL,            -- 'user' or 'assistant'
    content         TEXT NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_scope ON chat_messages(scope, created_at);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""


def init() -> None:
    """Crea il DB e le tabelle se non esistono ancora."""
    with _connect() as conn:
        conn.executescript(SCHEMA)


def get_meta(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ============= ACTIVITIES =============

def upsert_activity(activity: dict[str, Any]) -> None:
    """Inserisce o aggiorna un'attività. `activity` deve avere almeno id, start_time."""
    fields = [
        "id", "source", "type", "name", "start_time", "duration_sec", "moving_sec",
        "distance_m", "elev_gain_m", "elev_loss_m", "avg_pace_sec", "avg_hr", "max_hr",
        "avg_cadence", "max_cadence", "avg_power", "max_power", "avg_speed", "max_speed",
        "calories", "aerobic_te", "anaerobic_te", "training_load", "vo2max", "pte",
        "summary_json", "detail_json",
    ]
    values = [activity.get(f) for f in fields]
    placeholders = ",".join(["?"] * len(fields))
    field_list = ",".join(fields)
    update_list = ",".join(f"{f}=excluded.{f}" for f in fields if f != "id")
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO activities({field_list}) VALUES({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {update_list}",
            values,
        )


def list_activities(limit: int = 200, offset: int = 0, since: str | None = None) -> list[dict]:
    q = "SELECT * FROM activities"
    params: list = []
    if since:
        q += " WHERE start_time >= ?"
        params.append(since)
    q += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def get_activity(activity_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        return dict(row) if row else None


def delete_activity(activity_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))
        conn.execute("DELETE FROM chat_messages WHERE scope=?", (f"activity:{activity_id}",))


def monthly_totals(months_back: int = 12) -> list[dict]:
    """Restituisce km e ore per mese degli ultimi N mesi."""
    q = """
    SELECT
        substr(start_time, 1, 7) AS month,
        COUNT(*) AS n_activities,
        SUM(distance_m)/1000.0 AS km,
        SUM(moving_sec)/3600.0 AS hours
    FROM activities
    WHERE start_time IS NOT NULL
    GROUP BY month
    ORDER BY month DESC
    LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(q, (months_back,)).fetchall()
        return [dict(r) for r in rows]


# ============= SLEEP / WELLNESS / TRAINING =============

def upsert_row(table: str, fields: list[str], row: dict) -> None:
    placeholders = ",".join(["?"] * len(fields))
    field_list = ",".join(fields)
    update_list = ",".join(f"{f}=excluded.{f}" for f in fields if f != "date")
    values = [row.get(f) for f in fields]
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO {table}({field_list}) VALUES({placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {update_list}",
            values,
        )


SLEEP_FIELDS = [
    "date", "score", "duration_sec", "deep_sec", "light_sec", "rem_sec", "awake_sec",
    "avg_hrv", "avg_resp_rate", "avg_spo2", "avg_stress", "raw_json",
]

WELLNESS_FIELDS = [
    "date", "body_battery_high", "body_battery_low", "body_battery_charged",
    "body_battery_drained", "stress_avg", "stress_max", "hrv_status", "hrv_weekly_avg",
    "hrv_last_night", "resting_hr", "steps", "floors_climbed", "active_kcal", "raw_json",
]

TRAINING_FIELDS = [
    "date", "training_readiness", "training_status", "acute_load", "load_focus",
    "recovery_time_hours", "vo2max_running", "vo2max_cycling", "endurance_score",
    "hill_score", "race_predictor_5k", "race_predictor_10k", "race_predictor_half",
    "race_predictor_full", "raw_json",
]


def upsert_sleep(row: dict) -> None:
    upsert_row("sleep", SLEEP_FIELDS, row)


def upsert_wellness(row: dict) -> None:
    upsert_row("wellness", WELLNESS_FIELDS, row)


def upsert_training(row: dict) -> None:
    upsert_row("training", TRAINING_FIELDS, row)


def recent_wellness(days: int = 14) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wellness ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        return [dict(r) for r in rows]


def recent_sleep(days: int = 14) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sleep ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        return [dict(r) for r in rows]


def recent_training(days: int = 14) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM training ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============= CHAT =============

def add_chat(scope: str, role: str, content: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages(scope, role, content) VALUES(?,?,?)",
            (scope, role, content),
        )
        return cur.lastrowid or 0


def list_chat(scope: str, limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM chat_messages "
            "WHERE scope=? ORDER BY created_at ASC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_chat(scope: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE scope=?", (scope,))


# ============= STATS =============

def db_stats() -> dict:
    with _connect() as conn:
        def cnt(table: str) -> int:
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return {
            "activities": cnt("activities"),
            "sleep_nights": cnt("sleep"),
            "wellness_days": cnt("wellness"),
            "training_days": cnt("training"),
        }
