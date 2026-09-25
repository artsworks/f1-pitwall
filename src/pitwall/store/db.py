"""SQLite persistence for sessions, laps, calls, grades and bookmarks.

Migrations are an ordered list of SQL scripts tracked by PRAGMA user_version:
on open, every script with index >= user_version runs in its own transaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    """
    CREATE TABLE sessions (
        uid INTEGER PRIMARY KEY,
        track_id INT,
        session_type INT,
        started_at REAL,
        game_version TEXT,
        config_hash TEXT,
        weather INT,
        recording_path TEXT
    );
    CREATE TABLE laps (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        car_idx INT,
        lap_num INT,
        lap_time_ms INT,
        s1_ms INT,
        s2_ms INT,
        compound INT,
        tyre_age_laps INT,
        fuel_remaining_laps REAL,
        valid INT,
        invalid_reasons TEXT
    );
    CREATE TABLE stints (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        car_idx INT,
        compound INT,
        start_lap INT,
        end_lap INT,
        deg_params TEXT
    );
    CREATE TABLE pit_events (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        lap_num INT,
        loss_ms INT,
        neutralised INT
    );
    CREATE TABLE calls (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        call_id TEXT,
        t REAL,
        session_time REAL,
        lap INT,
        lap_distance REAL,
        rule_id TEXT,
        priority INT,
        outcome TEXT,
        suppressed_by TEXT,
        text TEXT,
        inputs TEXT,
        config_hash TEXT,
        mindset TEXT
    );
    CREATE INDEX calls_session_t ON calls(session_uid, t);
    CREATE TABLE call_grades (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        call_id TEXT,
        rule_id TEXT,
        grade TEXT CHECK(grade IN ('good','noise','too_late','wrong')),
        note TEXT,
        graded_at REAL,
        UNIQUE(session_uid, call_id)
    );
    CREATE TABLE bookmarks (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        t REAL,
        session_time REAL,
        lap INT,
        lap_distance REAL,
        note TEXT
    );
    CREATE TABLE track_params (
        track_id INTEGER PRIMARY KEY,
        pit_loss_s REAL,
        fuel_per_lap_kg REAL,
        base_pace_ms INT,
        updated_at REAL
    );
    """
]

# Decision-log outcomes that are persisted in `calls`; "bookmark" goes to
# `bookmarks`, everything else (paused/resumed/session_reset) stays JSONL-only.
_CALL_OUTCOMES = {"fired", "suppressed", "ack", "neg", "say_again", "quiet_until"}


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        file_backed = self.path != ":memory:"
        if file_backed:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: hub/websocket writes cross threads; callers
        # serialise access at a higher level.
        self._conn = sqlite3.connect(
            self.path if self.path == ":memory:" else str(Path(self.path).expanduser()),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        if file_backed:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    # -- migrations ---------------------------------------------------------

    def _version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def migrate(self) -> None:
        version = self._version()
        for i in range(version, len(MIGRATIONS)):
            with self._conn:
                self._conn.executescript(MIGRATIONS[i])
                self._conn.execute(f"PRAGMA user_version={i + 1}")

    def close(self) -> None:
        self._conn.close()

    # -- writes ---------------------------------------------------------------

    def upsert_session(
        self,
        uid: int,
        *,
        track_id: int = 0,
        session_type: int = 0,
        started_at: float | None = None,
        game_version: str = "",
        config_hash: str = "",
        weather: int = 0,
        recording_path: str = "",
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sessions(uid, track_id, session_type, started_at,"
                " game_version, config_hash, weather, recording_path)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uid) DO UPDATE SET"
                " track_id=excluded.track_id, session_type=excluded.session_type,"
                " game_version=excluded.game_version,"
                " config_hash=excluded.config_hash, weather=excluded.weather,"
                " recording_path=excluded.recording_path",
                (
                    uid,
                    track_id,
                    session_type,
                    started_at if started_at is not None else time.time(),
                    game_version,
                    config_hash,
                    weather,
                    recording_path,
                ),
            )

    def insert_lap(self, session_uid: int, car_idx: int, lap: Any) -> None:
        """Persist a state.lap.LapSummary (attribute access keeps it duck-typed)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO laps(session_uid, car_idx, lap_num, lap_time_ms,"
                " s1_ms, s2_ms, compound, tyre_age_laps, fuel_remaining_laps,"
                " valid, invalid_reasons) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_uid,
                    car_idx,
                    lap.lap_num,
                    lap.lap_time_ms,
                    lap.sector1_ms,
                    lap.sector2_ms,
                    lap.compound,
                    lap.tyre_age_laps,
                    lap.fuel_remaining_laps_at_end,
                    int(bool(lap.valid)),
                    json.dumps(list(lap.invalid_reasons)),
                ),
            )

    def insert_call(self, session_uid: int, record: dict[str, Any]) -> None:
        """Mirror a decision-log record: calls/bookmarks tables."""
        outcome = record.get("outcome")
        if outcome == "bookmark":
            with self._conn:
                self._conn.execute(
                    "INSERT INTO bookmarks(session_uid, t, session_time, lap,"
                    " lap_distance, note) VALUES(?,?,?,?,?,?)",
                    (
                        session_uid,
                        record.get("t"),
                        record.get("session_time"),
                        record.get("lap"),
                        record.get("lap_distance"),
                        record.get("note", ""),
                    ),
                )
            return
        if outcome not in _CALL_OUTCOMES:
            return
        inputs = record.get("inputs")
        with self._conn:
            self._conn.execute(
                "INSERT INTO calls(session_uid, call_id, t, session_time, lap,"
                " lap_distance, rule_id, priority, outcome, suppressed_by,"
                " text, inputs, config_hash, mindset)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_uid,
                    record.get("call_id"),
                    record.get("t"),
                    record.get("session_time"),
                    record.get("lap"),
                    record.get("lap_distance"),
                    record.get("rule_id"),
                    record.get("priority"),
                    outcome,
                    record.get("suppressed_by"),
                    record.get("text"),
                    json.dumps(inputs) if inputs is not None else None,
                    record.get("config_hash"),
                    record.get("mindset"),
                ),
            )

    def grade_call(
        self,
        session_uid: int,
        call_id: str,
        rule_id: str,
        grade: str,
        note: str = "",
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO call_grades(session_uid, call_id, rule_id, grade,"
                " note, graded_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(session_uid, call_id) DO UPDATE SET"
                " grade=excluded.grade, note=excluded.note,"
                " graded_at=excluded.graded_at",
                (session_uid, call_id, rule_id, grade, note, time.time()),
            )

    # -- reads ----------------------------------------------------------------

    def _rows(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]

    def calls_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM calls WHERE session_uid=? ORDER BY t", (uid,))

    def grades_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM call_grades WHERE session_uid=?", (uid,))

    def bookmarks_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM bookmarks WHERE session_uid=? ORDER BY t", (uid,))

    def latest_session_uid(self) -> int | None:
        row = self._conn.execute(
            "SELECT uid FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None


def open_configured(settings: Any) -> Database | None:
    """Open the persistence DB from settings; None when disabled."""
    if not settings.persistence.enabled:
        return None
    return Database(Path(settings.persistence.path).expanduser())


SessionUidSource = Callable[[], int]
