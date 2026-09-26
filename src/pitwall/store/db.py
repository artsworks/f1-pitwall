"""SQLite persistence for sessions, laps, calls, grades and bookmarks.

Migrations are an ordered list of SQL scripts tracked by PRAGMA user_version:
on open, every script with index >= user_version runs in its own transaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pitwall.model.deg import DegFit
    from pitwall.state.lap import LapSummary

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
    """,
    # Migration 2: M3 race engine (docs/18). All new columns nullable.
    """
    ALTER TABLE sessions ADD COLUMN game_mode INT;
    ALTER TABLE sessions ADD COLUMN ended_at REAL;
    ALTER TABLE laps ADD COLUMN wear_pct REAL;
    ALTER TABLE laps ADD COLUMN fuel_kg REAL;
    ALTER TABLE laps ADD COLUMN ers_deployed_j REAL;
    ALTER TABLE laps ADD COLUMN sc_status INT;
    ALTER TABLE laps ADD COLUMN weather INT;
    ALTER TABLE stints ADD COLUMN n_valid_laps INT;
    ALTER TABLE stints ADD COLUMN base_ms REAL;
    ALTER TABLE stints ADD COLUMN deg_ms_per_lap REAL;
    ALTER TABLE stints ADD COLUMN fuel_ms_per_lap REAL;
    ALTER TABLE stints ADD COLUMN rmse_ms REAL;
    ALTER TABLE stints ADD COLUMN updated_at REAL;
    ALTER TABLE pit_events ADD COLUMN lane_ms INT;
    ALTER TABLE pit_events ADD COLUMN in_lap_ms INT;
    ALTER TABLE pit_events ADD COLUMN out_lap_ms INT;
    ALTER TABLE pit_events ADD COLUMN ref_pace_ms INT;
    ALTER TABLE pit_events ADD COLUMN car_idx INT;
    CREATE TABLE model_params (
        track_id INT NOT NULL,
        compound INT NOT NULL,
        name TEXT NOT NULL,
        value REAL NOT NULL,
        weight REAL NOT NULL,
        updated_at REAL,
        PRIMARY KEY (track_id, compound, name)
    );
    CREATE TABLE ab_results (
        id INTEGER PRIMARY KEY,
        recorded_at REAL,
        recording TEXT,
        a_dir TEXT, b_dir TEXT, a_mindset TEXT, b_mindset TEXT,
        rule_id TEXT,
        only_a INT, only_b INT, both INT
    );
    CREATE TABLE runtime (
        key TEXT PRIMARY KEY,
        session_uid INT,
        session_t REAL,
        wall_t REAL,
        recording_path TEXT,
        lap_num INT
    );
    """,
]


@dataclass(frozen=True, slots=True)
class LapRow:
    id: int
    session_uid: int
    car_idx: int
    lap_num: int
    lap_time_ms: int
    s1_ms: int
    s2_ms: int
    compound: int
    tyre_age_laps: int
    fuel_remaining_laps: float
    valid: int
    invalid_reasons: tuple[str, ...]
    wear_pct: float
    fuel_kg: float
    ers_deployed_j: float
    sc_status: int
    weather: int


@dataclass(frozen=True, slots=True)
class StintRow:
    id: int
    session_uid: int
    car_idx: int
    compound: int
    start_lap: int
    end_lap: int
    n_valid_laps: int
    base_ms: float
    deg_ms_per_lap: float
    fuel_ms_per_lap: float
    rmse_ms: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class PitEventRow:
    id: int
    session_uid: int
    car_idx: int
    lap_num: int
    loss_ms: int
    neutralised: int
    lane_ms: int
    in_lap_ms: int
    out_lap_ms: int
    ref_pace_ms: int


@dataclass(frozen=True, slots=True)
class ModelParam:
    track_id: int
    compound: int
    name: str
    value: float
    weight: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Heartbeat:
    session_uid: int
    session_t: float
    wall_t: float
    recording_path: str
    lap_num: int


# Decision-log outcomes that are persisted in `calls`; "bookmark" goes to
# `bookmarks`, everything else (paused/resumed/session_reset) stays JSONL-only.
_CALL_OUTCOMES = {"fired", "suppressed", "ack", "neg", "say_again", "quiet_until", "quiet_off"}


_U64 = 1 << 64
_I64_MAX = (1 << 63) - 1
_UID_COLUMNS = ("uid", "session_uid")


def _uid_to_sql(uid: int) -> int:
    """Store the game's uint64 session UID in SQLite's signed 64-bit INTEGER."""
    return uid - _U64 if uid > _I64_MAX else uid


def _uid_from_sql(value: int) -> int:
    return value + _U64 if value < 0 else value


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
        game_mode: int = 0,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sessions(uid, track_id, session_type, started_at,"
                " game_version, config_hash, weather, recording_path, game_mode)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uid) DO UPDATE SET"
                " track_id=excluded.track_id, session_type=excluded.session_type,"
                " game_version=excluded.game_version,"
                " config_hash=excluded.config_hash, weather=excluded.weather,"
                " recording_path=excluded.recording_path,"
                " game_mode=excluded.game_mode",
                (
                    _uid_to_sql(uid),
                    track_id,
                    session_type,
                    started_at if started_at is not None else time.time(),
                    game_version,
                    config_hash,
                    weather,
                    recording_path,
                    game_mode,
                ),
            )

    def insert_lap(self, session_uid: int, car_idx: int, lap: LapSummary) -> None:
        """Persist a state.lap.LapSummary (attribute access keeps it duck-typed)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO laps(session_uid, car_idx, lap_num, lap_time_ms,"
                " s1_ms, s2_ms, compound, tyre_age_laps, fuel_remaining_laps,"
                " valid, invalid_reasons, wear_pct, fuel_kg, ers_deployed_j,"
                " sc_status, weather) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _uid_to_sql(session_uid),
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
                    lap.wear_pct,
                    lap.fuel_kg,
                    lap.ers_deployed_j,
                    lap.sc_status,
                    lap.weather,
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
                        _uid_to_sql(session_uid),
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
                    _uid_to_sql(session_uid),
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
                    json.dumps(inputs, default=str) if inputs is not None else None,
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
                (_uid_to_sql(session_uid), call_id, rule_id, grade, note, time.time()),
            )

    # -- reads ----------------------------------------------------------------

    def _rows(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        for row in rows:
            for column in _UID_COLUMNS:
                if isinstance(row.get(column), int):
                    row[column] = _uid_from_sql(row[column])
        return rows

    def calls_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM calls WHERE session_uid=? ORDER BY t", (_uid_to_sql(uid),))

    def grades_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM call_grades WHERE session_uid=?", (_uid_to_sql(uid),))

    def bookmarks_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM bookmarks WHERE session_uid=? ORDER BY t", (_uid_to_sql(uid),)
        )

    def latest_session_uid(self) -> int | None:
        row = self._conn.execute(
            "SELECT uid FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return _uid_from_sql(int(row[0])) if row else None

    # -- M3: laps / stints / pit events / model params (docs/18) --------------

    def laps_for(self, session_uid: int, car_idx: int = 0) -> list[LapRow]:
        rows = self._conn.execute(
            "SELECT * FROM laps WHERE session_uid=? AND car_idx=? ORDER BY lap_num, id",
            (_uid_to_sql(session_uid), car_idx),
        ).fetchall()
        return [self._lap_row(r) for r in rows]

    def _lap_row(self, r: sqlite3.Row) -> LapRow:
        return LapRow(
            id=int(r["id"]),
            session_uid=_uid_from_sql(int(r["session_uid"])),
            car_idx=int(r["car_idx"]),
            lap_num=int(r["lap_num"]),
            lap_time_ms=int(r["lap_time_ms"]),
            s1_ms=int(r["s1_ms"] or 0),
            s2_ms=int(r["s2_ms"] or 0),
            compound=int(r["compound"] or 0),
            tyre_age_laps=int(r["tyre_age_laps"] or 0),
            fuel_remaining_laps=float(r["fuel_remaining_laps"] or 0.0),
            valid=int(r["valid"] or 0),
            invalid_reasons=tuple(json.loads(r["invalid_reasons"] or "[]")),
            wear_pct=float(r["wear_pct"] or 0.0),
            fuel_kg=float(r["fuel_kg"] or 0.0),
            ers_deployed_j=float(r["ers_deployed_j"] or 0.0),
            sc_status=int(r["sc_status"] or 0),
            weather=int(r["weather"] or 0),
        )

    def upsert_stint(
        self,
        session_uid: int,
        car_idx: int,
        compound: int,
        start_lap: int,
        end_lap: int,
        fit: DegFit,
    ) -> None:
        """Insert or refresh the stint row keyed on (session, car, start_lap)."""
        uid = _uid_to_sql(session_uid)
        params = json.dumps(
            {
                "base_ms": fit.base_ms,
                "deg_ms_per_lap": fit.deg_ms_per_lap,
                "fuel_ms_per_lap": fit.fuel_ms_per_lap,
                "rmse_ms": fit.rmse_ms,
                "confidence": fit.confidence,
                "source": fit.source,
            }
        )
        with self._conn:
            cur = self._conn.execute(
                "UPDATE stints SET compound=?, end_lap=?, deg_params=?,"
                " n_valid_laps=?, base_ms=?, deg_ms_per_lap=?, fuel_ms_per_lap=?,"
                " rmse_ms=?, updated_at=?"
                " WHERE session_uid=? AND car_idx=? AND start_lap=?",
                (
                    compound,
                    end_lap,
                    params,
                    fit.n,
                    fit.base_ms,
                    fit.deg_ms_per_lap,
                    fit.fuel_ms_per_lap,
                    fit.rmse_ms,
                    time.time(),
                    uid,
                    car_idx,
                    start_lap,
                ),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO stints(session_uid, car_idx, compound, start_lap,"
                    " end_lap, deg_params, n_valid_laps, base_ms, deg_ms_per_lap,"
                    " fuel_ms_per_lap, rmse_ms, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid,
                        car_idx,
                        compound,
                        start_lap,
                        end_lap,
                        params,
                        fit.n,
                        fit.base_ms,
                        fit.deg_ms_per_lap,
                        fit.fuel_ms_per_lap,
                        fit.rmse_ms,
                        time.time(),
                    ),
                )

    def _stint_row(self, r: sqlite3.Row) -> StintRow:
        return StintRow(
            id=int(r["id"]),
            session_uid=_uid_from_sql(int(r["session_uid"])),
            car_idx=int(r["car_idx"]),
            compound=int(r["compound"]),
            start_lap=int(r["start_lap"]),
            end_lap=int(r["end_lap"] or 0),
            n_valid_laps=int(r["n_valid_laps"] or 0),
            base_ms=float(r["base_ms"] or 0.0),
            deg_ms_per_lap=float(r["deg_ms_per_lap"] or 0.0),
            fuel_ms_per_lap=float(r["fuel_ms_per_lap"] or 0.0),
            rmse_ms=float(r["rmse_ms"] or 0.0),
            updated_at=float(r["updated_at"] or 0.0),
        )

    def stints_for_track(self, track_id: int, compound: int, limit: int = 20) -> list[StintRow]:
        """Newest-first stints on this track/compound across all sessions."""
        rows = self._conn.execute(
            "SELECT st.* FROM stints st JOIN sessions se ON st.session_uid = se.uid"
            " WHERE se.track_id=? AND st.compound=? ORDER BY st.id DESC LIMIT ?",
            (track_id, compound, limit),
        ).fetchall()
        return [self._stint_row(r) for r in rows]

    def insert_pit_event(
        self,
        session_uid: int,
        car_idx: int,
        lap_num: int,
        loss_ms: int,
        neutralised: int,
        lane_ms: int,
        in_lap_ms: int,
        out_lap_ms: int,
        ref_pace_ms: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO pit_events(session_uid, lap_num, loss_ms, neutralised,"
                " lane_ms, in_lap_ms, out_lap_ms, ref_pace_ms, car_idx)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _uid_to_sql(session_uid),
                    lap_num,
                    loss_ms,
                    neutralised,
                    lane_ms,
                    in_lap_ms,
                    out_lap_ms,
                    ref_pace_ms,
                    car_idx,
                ),
            )

    def _pit_row(self, r: sqlite3.Row) -> PitEventRow:
        return PitEventRow(
            id=int(r["id"]),
            session_uid=_uid_from_sql(int(r["session_uid"])),
            car_idx=int(r["car_idx"] or 0),
            lap_num=int(r["lap_num"] or 0),
            loss_ms=int(r["loss_ms"] or 0),
            neutralised=int(r["neutralised"] or 0),
            lane_ms=int(r["lane_ms"] or 0),
            in_lap_ms=int(r["in_lap_ms"] or 0),
            out_lap_ms=int(r["out_lap_ms"] or 0),
            ref_pace_ms=int(r["ref_pace_ms"] or 0),
        )

    def pit_events_for_track(
        self, track_id: int, neutralised: int, limit: int = 20
    ) -> list[PitEventRow]:
        rows = self._conn.execute(
            "SELECT pe.* FROM pit_events pe JOIN sessions se ON pe.session_uid = se.uid"
            " WHERE se.track_id=? AND pe.neutralised=? ORDER BY pe.id DESC LIMIT ?",
            (track_id, neutralised, limit),
        ).fetchall()
        return [self._pit_row(r) for r in rows]

    def pit_events_for_session(self, session_uid: int) -> list[PitEventRow]:
        rows = self._conn.execute(
            "SELECT * FROM pit_events WHERE session_uid=? ORDER BY id",
            (_uid_to_sql(session_uid),),
        ).fetchall()
        return [self._pit_row(r) for r in rows]

    def get_param(self, track_id: int, compound: int, name: str) -> ModelParam | None:
        r = self._conn.execute(
            "SELECT * FROM model_params WHERE track_id=? AND compound=? AND name=?",
            (track_id, compound, name),
        ).fetchone()
        return self._param_row(r) if r is not None else None

    def fold_param(
        self,
        track_id: int,
        compound: int,
        name: str,
        value: float,
        weight: float = 1.0,
        *,
        param_weight_cap: float = 50.0,
    ) -> ModelParam:
        """Weighted running mean in model_params; weight capped at the cap."""
        old = self.get_param(track_id, compound, name)
        old_w = old.weight if old is not None else 0.0
        old_v = old.value if old is not None else 0.0
        total_w = old_w + weight
        new_v = (old_v * old_w + value * weight) / total_w if total_w > 0 else value
        new_w = min(total_w, param_weight_cap)
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO model_params(track_id, compound, name, value, weight,"
                " updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(track_id, compound, name) DO UPDATE SET"
                " value=excluded.value, weight=excluded.weight,"
                " updated_at=excluded.updated_at",
                (track_id, compound, name, new_v, new_w, now),
            )
        return ModelParam(track_id, compound, name, new_v, new_w, now)

    def _param_row(self, r: sqlite3.Row) -> ModelParam:
        return ModelParam(
            track_id=int(r["track_id"]),
            compound=int(r["compound"]),
            name=str(r["name"]),
            value=float(r["value"]),
            weight=float(r["weight"]),
            updated_at=float(r["updated_at"] or 0.0),
        )

    def set_param(
        self, track_id: int, compound: int, name: str, value: float, weight: float
    ) -> ModelParam:
        """Overwrite a model_params row (recomputed values, e.g. pitwall tune)."""
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO model_params(track_id, compound, name, value, weight,"
                " updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(track_id, compound, name) DO UPDATE SET"
                " value=excluded.value, weight=excluded.weight,"
                " updated_at=excluded.updated_at",
                (track_id, compound, name, value, weight, now),
            )
        return ModelParam(track_id, compound, name, value, weight, now)

    def all_grades(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM call_grades ORDER BY graded_at", ())

    def ab_results(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM ab_results ORDER BY recorded_at", ())

    def params_for_track(self, track_id: int) -> list[ModelParam]:
        rows = self._conn.execute(
            "SELECT * FROM model_params WHERE track_id=? ORDER BY compound, name",
            (track_id,),
        ).fetchall()
        return [self._param_row(r) for r in rows]

    def record_ab(
        self,
        *,
        recording: str = "",
        a_dir: str = "",
        b_dir: str = "",
        a_mindset: str = "",
        b_mindset: str = "",
        rule_id: str = "",
        only_a: int = 0,
        only_b: int = 0,
        both: int = 0,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO ab_results(recorded_at, recording, a_dir, b_dir,"
                " a_mindset, b_mindset, rule_id, only_a, only_b, both)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    recording,
                    a_dir,
                    b_dir,
                    a_mindset,
                    b_mindset,
                    rule_id,
                    only_a,
                    only_b,
                    both,
                ),
            )

    def write_heartbeat(
        self,
        session_uid: int,
        session_t: float,
        wall_t: float,
        recording_path: str,
        lap_num: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runtime(key, session_uid, session_t, wall_t,"
                " recording_path, lap_num) VALUES('heartbeat',?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " session_uid=excluded.session_uid, session_t=excluded.session_t,"
                " wall_t=excluded.wall_t, recording_path=excluded.recording_path,"
                " lap_num=excluded.lap_num",
                (_uid_to_sql(session_uid), session_t, wall_t, recording_path, lap_num),
            )

    def read_heartbeat(self) -> Heartbeat | None:
        r = self._conn.execute("SELECT * FROM runtime WHERE key='heartbeat'").fetchone()
        if r is None:
            return None
        return Heartbeat(
            session_uid=_uid_from_sql(int(r["session_uid"] or 0)),
            session_t=float(r["session_t"] or 0.0),
            wall_t=float(r["wall_t"] or 0.0),
            recording_path=str(r["recording_path"] or ""),
            lap_num=int(r["lap_num"] or 0),
        )

    def end_session(self, uid: int, ended_at: float) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET ended_at=? WHERE uid=?", (ended_at, _uid_to_sql(uid))
            )


def open_configured(settings: Any) -> Database | None:
    """Open the persistence DB from settings; None when disabled."""
    if not settings.persistence.enabled:
        return None
    return Database(Path(settings.persistence.path).expanduser())


SessionUidSource = Callable[[], int]
