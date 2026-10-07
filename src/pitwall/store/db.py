"""SQLite persistence for sessions, laps, calls, grades and bookmarks.

Migrations are an ordered list of SQL scripts tracked by PRAGMA user_version:
on open, every script with index >= user_version runs in its own transaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pitwall.derive import is_synthetic_uid
from pitwall.protocol.enums import session_kind

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
    # 3: driver menu picks (docs/12): questions, opinions, actions
    """
    CREATE TABLE driver_inputs (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        t REAL,
        session_time REAL,
        lap INT,
        lap_distance REAL,
        item_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        topic TEXT,
        label TEXT,
        reply TEXT,
        inputs TEXT,
        mindset TEXT
    );
    CREATE INDEX driver_inputs_session ON driver_inputs(session_uid, t);
    """,
    # 4: named strategy plans (docs/03, docs/18): active plan on every call and
    # the set/switch/off-plan history, for review grading of plan calls.
    """
    ALTER TABLE calls ADD COLUMN active_plan TEXT;
    ALTER TABLE calls ADD COLUMN on_plan INT;
    CREATE TABLE plan_events (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        t REAL,
        session_time REAL,
        lap INT,
        kind TEXT,
        from_plan TEXT,
        to_plan TEXT,
        reason TEXT,
        delta_s REAL,
        sequence TEXT,
        plans TEXT
    );
    CREATE INDEX plan_events_session ON plan_events(session_uid, t);
    """,
    # 6: hindsight outcomes (pitwall digest): automatic labels for fired calls
    # and plan events against what actually happened; recomputed per session.
    """
    CREATE TABLE outcomes (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        call_id TEXT,
        rule_id TEXT,
        lap INT,
        metric TEXT,
        predicted REAL,
        actual REAL,
        error REAL,
        label TEXT,
        detail TEXT
    );
    CREATE INDEX outcomes_session ON outcomes(session_uid, lap);
    """,
    # 7: race distance, so the hindsight grader can tell a finished session
    # from a partial / aborted / retired one (censored labels).
    """
    ALTER TABLE sessions ADD COLUMN total_laps INT NOT NULL DEFAULT 0;
    """,
    # 8: M4 learning metadata and tyre temperatures.
    """
    ALTER TABLE sessions ADD COLUMN weekend_link INT DEFAULT 0;
    ALTER TABLE sessions ADD COLUMN calls_mode TEXT DEFAULT '';
    ALTER TABLE laps ADD COLUMN tyre_inner_c REAL DEFAULT 0;
    ALTER TABLE laps ADD COLUMN tyre_surface_c REAL DEFAULT 0;
    CREATE TABLE ingested (
        session_uid INTEGER PRIMARY KEY,
        path TEXT,
        digest_version INT,
        ingested_at REAL
    );
    """,
    # 9: automatic maintenance: learned values that failed a sanity check or
    # were recomputed keep their last value here instead of being lost.
    """
    CREATE TABLE model_params_quarantine (
        track_id INT NOT NULL,
        compound INT NOT NULL,
        name TEXT NOT NULL,
        reason TEXT NOT NULL,
        value REAL NOT NULL,
        weight REAL NOT NULL,
        updated_at REAL,
        quarantined_at REAL,
        PRIMARY KEY (track_id, compound, name, reason)
    );
    CREATE TABLE maintenance (
        key TEXT PRIMARY KEY,
        version INT NOT NULL,
        ran_at REAL
    );
    """,
    # 10: setup advisor A1 (docs/22 §5).
    """
    CREATE TABLE setup_states (
        id INTEGER PRIMARY KEY,
        hash TEXT UNIQUE,
        fields TEXT
    );
    CREATE TABLE setup_changes (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        lap INT,
        session_time REAL,
        from_state INT,
        to_state INT
    );
    CREATE UNIQUE INDEX setup_changes_key
        ON setup_changes(session_uid, session_time, to_state);
    CREATE TABLE setup_recs (
        id INTEGER PRIMARY KEY,
        session_uid INT,
        rec_id TEXT UNIQUE,
        rule_id TEXT,
        mode TEXT,
        param TEXT,
        from_value REAL,
        delta REAL,
        conf TEXT,
        setup_state_id INT,
        track_id INT,
        compound INT,
        lap INT,
        evidence TEXT,
        folded INT DEFAULT 0
    );
    ALTER TABLE stints ADD COLUMN setup_state_id INT;
    ALTER TABLE sessions ADD COLUMN parc_ferme INT;
    ALTER TABLE sessions ADD COLUMN setup_folded INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN setup_state_id INT;
    ALTER TABLE laps ADD COLUMN traction_exits INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN slip_balance_deg REAL DEFAULT 0;
    ALTER TABLE laps ADD COLUMN slip_samples INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN lockups_front INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN lockups_rear INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN snaps_entry INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN snaps_exit INT DEFAULT 0;
    ALTER TABLE laps ADD COLUMN wear_front_pct REAL DEFAULT 0;
    ALTER TABLE laps ADD COLUMN wear_rear_pct REAL DEFAULT 0;
    ALTER TABLE laps ADD COLUMN tyre_inner_front_c REAL DEFAULT 0;
    ALTER TABLE laps ADD COLUMN tyre_inner_rear_c REAL DEFAULT 0;
    """,
    # 11: press grades and contextual bookmarks.
    """
    ALTER TABLE call_grades ADD COLUMN source TEXT NOT NULL DEFAULT 'human';
    ALTER TABLE bookmarks ADD COLUMN kind TEXT NOT NULL DEFAULT 'hold';
    ALTER TABLE bookmarks ADD COLUMN context TEXT;
    """,
    # 12: synthetic sessions from pitwall derive.
    """
    ALTER TABLE sessions ADD COLUMN synthetic INT DEFAULT 0;
    ALTER TABLE sessions ADD COLUMN derived_from TEXT DEFAULT '';
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
    tyre_inner_c: float = 0.0
    wear_front_pct: float = 0.0
    wear_rear_pct: float = 0.0
    tyre_inner_front_c: float = 0.0
    tyre_inner_rear_c: float = 0.0
    tyre_surface_c: float = 0.0
    visual: int = 0
    setup_state_id: int | None = None
    traction_exits: int = 0
    slip_balance_deg: float = 0.0
    slip_samples: int = 0
    lockups_front: int = 0
    lockups_rear: int = 0
    snaps_entry: int = 0
    snaps_exit: int = 0


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
    setup_state_id: int | None = None


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


@dataclass(frozen=True, slots=True)
class WeekendStint:
    session_uid: int
    started_at: float
    n_valid_laps: int
    deg_ms_per_lap: float
    fuel_ms_per_lap: float | None = None  # prior fuel slope the fit assumed; None if fitted


# Decision-log outcomes that are persisted in `calls`; "bookmark" goes to
# `bookmarks`, everything else (paused/resumed/session_reset) stays JSONL-only.
_CALL_OUTCOMES = {"fired", "suppressed", "ack", "neg", "say_again", "quiet_until", "quiet_off"}


_U64 = 1 << 64
_I64_MAX = (1 << 63) - 1
_UID_COLUMNS = ("uid", "session_uid")


def _uid_to_sql(uid: int) -> int:
    """Store the game's uint64 session UID in SQLite's signed 64-bit INTEGER."""
    return uid - _U64 if uid > _I64_MAX else uid


def _bool_to_sql(value: object) -> int | None:
    return int(value) if isinstance(value, bool) else None


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
        self._transaction_id = 0
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
        self._ensure_column("laps", "visual", "INT DEFAULT 0")

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add a nullable column when it is missing. Idempotent, so a database
        that already has the column from another build is left alone."""
        cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            with self._conn:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._transaction_id += 1
        name = f"pitwall_{self._transaction_id}"
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self._conn.execute(f"ROLLBACK TO {name}")
            self._conn.execute(f"RELEASE {name}")
            raise
        else:
            self._conn.execute(f"RELEASE {name}")

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
        weekend_link: int = 0,
        calls_mode: str = "",
        parc_ferme: int | None = None,
        synthetic: bool = False,
    ) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO sessions(uid, track_id, session_type, started_at,"
                " game_version, config_hash, weather, recording_path, game_mode,"
                " weekend_link, calls_mode, parc_ferme, synthetic)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uid) DO UPDATE SET"
                " track_id=excluded.track_id, session_type=excluded.session_type,"
                " game_version=excluded.game_version,"
                " config_hash=excluded.config_hash, weather=excluded.weather,"
                " recording_path=excluded.recording_path,"
                " game_mode=excluded.game_mode, weekend_link=excluded.weekend_link,"
                " calls_mode=excluded.calls_mode,"
                " parc_ferme=COALESCE(excluded.parc_ferme, sessions.parc_ferme),"
                " synthetic=MAX(COALESCE(sessions.synthetic, 0), excluded.synthetic)",
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
                    weekend_link,
                    calls_mode,
                    parc_ferme,
                    int(synthetic),
                ),
            )

    def insert_lap(
        self,
        session_uid: int,
        car_idx: int,
        lap: LapSummary,
        *,
        setup_state_id: int | None = None,
    ) -> None:
        """Persist a state.lap.LapSummary (attribute access keeps it duck-typed)."""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO laps(session_uid, car_idx, lap_num, lap_time_ms,"
                " s1_ms, s2_ms, compound, tyre_age_laps, fuel_remaining_laps,"
                " valid, invalid_reasons, wear_pct, fuel_kg, ers_deployed_j,"
                " sc_status, weather, tyre_inner_c, tyre_surface_c, visual,"
                " setup_state_id, traction_exits, slip_balance_deg, slip_samples,"
                " lockups_front, lockups_rear, snaps_entry, snaps_exit,"
                " wear_front_pct, wear_rear_pct, tyre_inner_front_c, tyre_inner_rear_c)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    lap.tyre_inner_c,
                    lap.tyre_surface_c,
                    lap.visual,
                    setup_state_id,
                    getattr(lap, "traction_exits", 0),
                    getattr(lap, "slip_balance_deg", 0.0),
                    getattr(lap, "slip_samples", 0),
                    getattr(lap, "lockups_front", 0),
                    getattr(lap, "lockups_rear", 0),
                    getattr(lap, "snaps_entry", 0),
                    getattr(lap, "snaps_exit", 0),
                    getattr(lap, "wear_front_pct", 0.0),
                    getattr(lap, "wear_rear_pct", 0.0),
                    getattr(lap, "tyre_inner_front_c", 0.0),
                    getattr(lap, "tyre_inner_rear_c", 0.0),
                ),
            )

    def insert_call(self, session_uid: int, record: dict[str, Any]) -> None:
        """Mirror a decision-log record: calls/bookmarks tables."""
        outcome = record.get("outcome")
        if outcome == "bookmark":
            with self.transaction():
                self._conn.execute(
                    "INSERT INTO bookmarks(session_uid, t, session_time, lap,"
                    " lap_distance, note, kind, context) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        _uid_to_sql(session_uid),
                        record.get("t"),
                        record.get("session_time"),
                        record.get("lap"),
                        record.get("lap_distance"),
                        record.get("note", ""),
                        record.get("kind") or "hold",
                        json.dumps(record.get("context"), default=str)
                        if record.get("context") is not None
                        else None,
                    ),
                )
            return
        if outcome == "driver_input":
            self._insert_driver_input(session_uid, record)
            return
        if outcome not in _CALL_OUTCOMES:
            return
        inputs = record.get("inputs")
        with self.transaction():
            self._conn.execute(
                "INSERT INTO calls(session_uid, call_id, t, session_time, lap,"
                " lap_distance, rule_id, priority, outcome, suppressed_by,"
                " text, inputs, config_hash, mindset, active_plan, on_plan)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    record.get("active_plan") or None,
                    _bool_to_sql(record.get("on_plan")),
                ),
            )
        if (
            outcome in ("ack", "neg")
            and record.get("call_id")
            and record.get("grade")
            and not is_synthetic_uid(session_uid)
        ):
            grade_call_id = record.get("grade_call_id") or record["call_id"]
            self.grade_call(
                session_uid,
                str(grade_call_id),
                str(record.get("rule_id") or ""),
                str(record["grade"]),
                source="press",
            )

    def insert_plan_event(self, session_uid: int, record: dict[str, Any]) -> None:
        """A named-plan set / switch / off / on event (engine plan tracker)."""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO plan_events(session_uid, t, session_time, lap, kind,"
                " from_plan, to_plan, reason, delta_s, sequence, plans)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _uid_to_sql(session_uid),
                    record.get("t"),
                    record.get("session_time"),
                    record.get("lap"),
                    record.get("kind"),
                    record.get("from_plan"),
                    record.get("to_plan"),
                    record.get("reason"),
                    record.get("delta_s"),
                    record.get("sequence"),
                    json.dumps(record.get("plans", []), default=str),
                ),
            )

    def _insert_driver_input(self, session_uid: int, record: dict[str, Any]) -> None:
        inputs = record.get("inputs")
        with self.transaction():
            self._conn.execute(
                "INSERT INTO driver_inputs(session_uid, t, session_time, lap, lap_distance,"
                " item_id, kind, topic, label, reply, inputs, mindset)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _uid_to_sql(session_uid),
                    record.get("t"),
                    record.get("session_time"),
                    record.get("lap"),
                    record.get("lap_distance"),
                    record.get("item_id"),
                    record.get("kind"),
                    record.get("topic") or None,
                    record.get("label"),
                    record.get("text"),
                    json.dumps(inputs, default=str) if inputs is not None else None,
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
        source: Literal["human", "press"] = "human",
    ) -> None:
        with self.transaction():
            if source == "press":
                self._conn.execute(
                    "INSERT INTO call_grades(session_uid, call_id, rule_id, grade,"
                    " note, graded_at, source) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_uid, call_id) DO UPDATE SET"
                    " rule_id=excluded.rule_id, grade=excluded.grade, note=excluded.note,"
                    " graded_at=excluded.graded_at"
                    " WHERE call_grades.source='press'",
                    (
                        _uid_to_sql(session_uid),
                        call_id,
                        rule_id,
                        grade,
                        note,
                        time.time(),
                        source,
                    ),
                )
            else:
                self._conn.execute(
                    "INSERT INTO call_grades(session_uid, call_id, rule_id, grade,"
                    " note, graded_at, source) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_uid, call_id) DO UPDATE SET"
                    " rule_id=excluded.rule_id, grade=excluded.grade, note=excluded.note,"
                    " graded_at=excluded.graded_at, source='human'",
                    (
                        _uid_to_sql(session_uid),
                        call_id,
                        rule_id,
                        grade,
                        note,
                        time.time(),
                        source,
                    ),
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

    def plan_events_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM plan_events WHERE session_uid=? ORDER BY id", (_uid_to_sql(uid),)
        )

    def replace_outcomes(self, session_uid: int, rows: list[dict[str, Any]]) -> None:
        """Replace a session's hindsight outcomes (recomputed as a whole)."""
        uid = _uid_to_sql(session_uid)
        with self.transaction():
            self._conn.execute("DELETE FROM outcomes WHERE session_uid=?", (uid,))
            self._conn.executemany(
                "INSERT INTO outcomes(session_uid, call_id, rule_id, lap, metric,"
                " predicted, actual, error, label, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        uid,
                        r.get("call_id"),
                        r.get("rule_id"),
                        r.get("lap"),
                        r.get("metric"),
                        r.get("predicted"),
                        r.get("actual"),
                        r.get("error"),
                        r.get("label"),
                        r.get("detail", ""),
                    )
                    for r in rows
                ],
            )

    def outcomes_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM outcomes WHERE session_uid=? ORDER BY lap, id", (_uid_to_sql(uid),)
        )

    def all_outcomes(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM outcomes ORDER BY session_uid, lap, id", ())

    def session_row(self, uid: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM sessions WHERE uid=?", (_uid_to_sql(uid),))
        return rows[0] if rows else None

    def set_session_total_laps(self, uid: int, total_laps: int) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE sessions SET total_laps=? WHERE uid=?", (total_laps, _uid_to_sql(uid))
            )

    def set_session_parc_ferme(self, uid: int, value: int) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE sessions SET parc_ferme=? WHERE uid=?", (value, _uid_to_sql(uid))
            )

    def setup_state_id(self, hash: str, fields: Mapping[str, Any] | None = None) -> int:
        """Get or insert a setup-state row, keyed by its stable hash."""
        encoded = json.dumps(dict(fields)) if fields is not None else None
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO setup_states(hash, fields) VALUES(?,?)",
                (hash, encoded),
            )
            if encoded is not None:
                self._conn.execute(
                    "UPDATE setup_states SET fields=COALESCE(fields,?) WHERE hash=?",
                    (encoded, hash),
                )
            row = self._conn.execute("SELECT id FROM setup_states WHERE hash=?", (hash,)).fetchone()
        if row is None:
            raise RuntimeError("setup state insert did not produce a row")
        return int(row["id"])

    def setup_state_fields(self, state_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT fields FROM setup_states WHERE id=?", (state_id,)
        ).fetchone()
        if row is None or row["fields"] is None:
            return None
        try:
            fields = json.loads(str(row["fields"]))
        except json.JSONDecodeError:
            return None
        return fields if isinstance(fields, dict) else None

    def insert_setup_change(
        self,
        session_uid: int,
        lap: int,
        session_time: float,
        from_state: int | None,
        to_state: int,
    ) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO setup_changes"
                "(session_uid, lap, session_time, from_state, to_state) VALUES(?,?,?,?,?)",
                (_uid_to_sql(session_uid), lap, session_time, from_state, to_state),
            )

    def delete_setup_changes_after(self, uid: int, session_time: float) -> None:
        """Drop setup changes a flashback undid (recorded after session_time)."""
        with self.transaction():
            self._conn.execute(
                "DELETE FROM setup_changes WHERE session_uid=? AND session_time>?",
                (_uid_to_sql(uid), session_time),
            )

    def setup_changes_for_session(self, uid: int) -> list[dict[str, Any]]:
        changes = self._rows(
            "SELECT * FROM setup_changes WHERE session_uid=? ORDER BY session_time, id",
            (_uid_to_sql(uid),),
        )
        for change in changes:
            change["session_uid"] = _uid_from_sql(int(change["session_uid"]))
        return changes

    def insert_setup_rec(
        self,
        rec: Any,
        *,
        track_id: int,
        compound: int,
        lap: int,
    ) -> None:
        """Replace a stored recommendation with the same deterministic id."""
        evidence = {
            "signals": rec.evidence,
            "suppressed": rec.suppressed,
            "tier": rec.tier,
            "expect": rec.expect,
            "tradeoff": rec.tradeoff,
            "to_value": rec.to_value,
            "session_type": rec.session_type,
            "parc_ferme": rec.parc_ferme,
        }
        session_uid = getattr(rec, "session_uid", None)
        if session_uid is None:
            try:
                session_uid = int(rec.rec_id.split(":", 1)[0])
            except (AttributeError, ValueError) as exc:
                raise ValueError("recommendation rec_id must start with its session uid") from exc
        with self.transaction():
            self._conn.execute(
                "INSERT INTO setup_recs"
                "(session_uid, rec_id, rule_id, mode, param, from_value, delta, conf,"
                " setup_state_id, track_id, compound, lap, evidence)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(rec_id) DO UPDATE SET"
                " session_uid=excluded.session_uid, rule_id=excluded.rule_id,"
                " mode=excluded.mode, param=excluded.param,"
                " from_value=excluded.from_value, delta=excluded.delta, conf=excluded.conf,"
                " setup_state_id=excluded.setup_state_id, track_id=excluded.track_id,"
                " compound=excluded.compound, lap=excluded.lap, evidence=excluded.evidence",
                (
                    _uid_to_sql(session_uid),
                    rec.rec_id,
                    rec.rule_id,
                    rec.mode,
                    rec.param,
                    rec.from_value,
                    rec.delta,
                    rec.conf,
                    rec.setup_state_id,
                    track_id,
                    compound,
                    lap,
                    json.dumps(evidence, default=str),
                ),
            )

    def setup_rec_by_id(self, rec_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM setup_recs WHERE rec_id=?", (rec_id,))
        if not rows:
            return None
        row = rows[0]
        try:
            evidence = json.loads(str(row["evidence"] or "{}"))
        except json.JSONDecodeError:
            evidence = {}
        row["evidence"] = evidence if isinstance(evidence, dict) else {}
        return row

    def mark_setup_rec_folded(self, rec_id: str) -> bool:
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE setup_recs SET folded=1 WHERE rec_id=? AND COALESCE(folded, 0)=0",
                (rec_id,),
            )
        return cursor.rowcount > 0

    def mark_setup_session_folded(self, uid: int) -> bool:
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE sessions SET setup_folded=1 WHERE uid=? AND COALESCE(setup_folded, 0)=0",
                (_uid_to_sql(uid),),
            )
        return cursor.rowcount > 0

    def setup_recs_for_session(self, uid: int) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM setup_recs WHERE session_uid=? ORDER BY id",
            (_uid_to_sql(uid),),
        )
        for row in rows:
            try:
                evidence = json.loads(str(row["evidence"] or "{}"))
            except json.JSONDecodeError:
                evidence = {}
            row["evidence"] = evidence if isinstance(evidence, dict) else {}
        return rows

    def grades_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM call_grades WHERE session_uid=?", (_uid_to_sql(uid),))

    def bookmarks_for_session(self, uid: int) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM bookmarks WHERE session_uid=? ORDER BY t", (_uid_to_sql(uid),)
        )
        for row in rows:
            try:
                context = json.loads(str(row["context"] or "{}"))
            except json.JSONDecodeError:
                context = {}
            row["context"] = context if isinstance(context, dict) else {}
        return rows

    def driver_inputs_for_session(self, uid: int) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM driver_inputs WHERE session_uid=? ORDER BY t", (_uid_to_sql(uid),)
        )

    def latest_session_uid(self) -> int | None:
        row = self._conn.execute(
            "SELECT uid FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return _uid_from_sql(int(row[0])) if row else None

    def latest_session_with_laps_uid(self) -> int | None:
        row = self._conn.execute(
            "SELECT s.uid FROM sessions s WHERE EXISTS("
            "SELECT 1 FROM laps l WHERE l.session_uid=s.uid AND l.car_idx=0)"
            " ORDER BY s.started_at DESC LIMIT 1"
        ).fetchone()
        return _uid_from_sql(int(row[0])) if row else None

    # -- M3: laps / stints / pit events / model params (docs/18) --------------

    def laps_for(self, session_uid: int, car_idx: int = 0) -> list[LapRow]:
        rows = self._conn.execute(
            "SELECT * FROM laps WHERE session_uid=? AND car_idx=? ORDER BY lap_num, id",
            (_uid_to_sql(session_uid), car_idx),
        ).fetchall()
        return [self._lap_row(r) for r in rows]

    def track_minutes(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(lap_time_ms), 0) AS total_ms, COUNT(*) AS laps,"
            " COUNT(DISTINCT session_uid) AS sessions FROM laps WHERE id IN ("
            "SELECT MAX(id) FROM laps WHERE car_idx=0 AND lap_time_ms>0"
            " GROUP BY session_uid, lap_num)"
        ).fetchone()
        return {
            "minutes": round(int(row["total_ms"]) / 60_000, 1),
            "laps": int(row["laps"]),
            "sessions": int(row["sessions"]),
        }

    def session_track_ms(self) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT l.session_uid AS uid, s.started_at, s.track_id, s.session_type,"
            " COUNT(*) AS laps, SUM(l.lap_time_ms) AS ms FROM laps l"
            " LEFT JOIN sessions s ON s.uid=l.session_uid WHERE l.id IN ("
            "SELECT MAX(id) FROM laps WHERE car_idx=0 AND lap_time_ms>0"
            " GROUP BY session_uid, lap_num)"
            " GROUP BY l.session_uid ORDER BY s.started_at, l.session_uid",
            (),
        )
        for row in rows:
            row["uid"] = _uid_from_sql(int(row["uid"]))
        return rows

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
            tyre_inner_c=float(r["tyre_inner_c"] or 0.0),
            wear_front_pct=float(r["wear_front_pct"] or 0.0),
            wear_rear_pct=float(r["wear_rear_pct"] or 0.0),
            tyre_inner_front_c=float(r["tyre_inner_front_c"] or 0.0),
            tyre_inner_rear_c=float(r["tyre_inner_rear_c"] or 0.0),
            tyre_surface_c=float(r["tyre_surface_c"] or 0.0),
            visual=int(r["visual"] or 0),
            setup_state_id=(int(r["setup_state_id"]) if r["setup_state_id"] is not None else None),
            traction_exits=int(r["traction_exits"] or 0),
            slip_balance_deg=float(r["slip_balance_deg"] or 0.0),
            slip_samples=int(r["slip_samples"] or 0),
            lockups_front=int(r["lockups_front"] or 0),
            lockups_rear=int(r["lockups_rear"] or 0),
            snaps_entry=int(r["snaps_entry"] or 0),
            snaps_exit=int(r["snaps_exit"] or 0),
        )

    def upsert_stint(
        self,
        session_uid: int,
        car_idx: int,
        compound: int,
        start_lap: int,
        end_lap: int,
        fit: DegFit,
        *,
        setup_state_id: int | None = None,
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
                "fuel_fitted": fit.fuel_fitted,
            }
        )
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE stints SET compound=?, end_lap=?, deg_params=?,"
                " n_valid_laps=?, base_ms=?, deg_ms_per_lap=?, fuel_ms_per_lap=?,"
                " rmse_ms=?, updated_at=?, setup_state_id=?"
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
                    setup_state_id,
                    uid,
                    car_idx,
                    start_lap,
                ),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO stints(session_uid, car_idx, compound, start_lap,"
                    " end_lap, deg_params, n_valid_laps, base_ms, deg_ms_per_lap,"
                    " fuel_ms_per_lap, rmse_ms, updated_at, setup_state_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        setup_state_id,
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
            setup_state_id=(int(r["setup_state_id"]) if r["setup_state_id"] is not None else None),
        )

    def stints_for_track(self, track_id: int, compound: int, limit: int = 20) -> list[StintRow]:
        """Newest-first stints on this track/compound across all sessions."""
        rows = self._conn.execute(
            "SELECT st.* FROM stints st JOIN sessions se ON st.session_uid = se.uid"
            " WHERE se.track_id=? AND st.compound=? ORDER BY st.id DESC LIMIT ?",
            (track_id, compound, limit),
        ).fetchall()
        return [self._stint_row(r) for r in rows]

    def stints_for_session(self, uid: int) -> list[StintRow]:
        rows = self._conn.execute(
            "SELECT * FROM stints WHERE session_uid=? AND car_idx=0 ORDER BY start_lap",
            (_uid_to_sql(uid),),
        ).fetchall()
        return [self._stint_row(row) for row in rows]

    def weekend_stints(self, uid: int, track_id: int, compound: int) -> list[WeekendStint]:
        """Earlier fitted practice stints in the same track weekend."""
        current = self.session_row(uid)
        if current is None:
            return []
        started_at = float(current.get("started_at") or 0.0)
        weekend_link = int(current.get("weekend_link") or 0)
        started_date = datetime.fromtimestamp(started_at, UTC).date()
        rows = self._conn.execute(
            "SELECT se.uid, se.started_at, se.weekend_link, se.session_type,"
            " st.n_valid_laps, st.deg_params FROM stints st"
            " JOIN sessions se ON se.uid=st.session_uid"
            " WHERE se.track_id=? AND st.compound=? AND se.uid<>?"
            " AND COALESCE(se.synthetic, 0)=0"
            " AND se.started_at<? ORDER BY se.started_at, st.start_lap",
            (track_id, compound, _uid_to_sql(uid), started_at),
        ).fetchall()
        result: list[WeekendStint] = []
        for row in rows:
            try:
                if session_kind(int(row["session_type"])) != "practice":
                    continue
            except ValueError:
                continue
            prior_link = int(row["weekend_link"] or 0)
            if weekend_link and prior_link:
                same_weekend = weekend_link == prior_link
            else:
                prior_date = datetime.fromtimestamp(float(row["started_at"] or 0.0), UTC).date()
                same_weekend = started_date == prior_date
            if not same_weekend:
                continue
            params = json.loads(str(row["deg_params"] or "{}"))
            if params.get("source") != "fit":
                continue
            result.append(
                WeekendStint(
                    session_uid=_uid_from_sql(int(row["uid"])),
                    started_at=float(row["started_at"] or 0.0),
                    n_valid_laps=int(row["n_valid_laps"] or 0),
                    deg_ms_per_lap=float(params.get("deg_ms_per_lap", 0.0)),
                    fuel_ms_per_lap=(
                        float(params["fuel_ms_per_lap"])
                        if "fuel_ms_per_lap" in params and not params.get("fuel_fitted")
                        else None
                    ),
                )
            )
        return result

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
        with self.transaction():
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
        with self.transaction():
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
        with self.transaction():
            self._conn.execute(
                "INSERT INTO model_params(track_id, compound, name, value, weight,"
                " updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(track_id, compound, name) DO UPDATE SET"
                " value=excluded.value, weight=excluded.weight,"
                " updated_at=excluded.updated_at",
                (track_id, compound, name, value, weight, now),
            )
        return ModelParam(track_id, compound, name, value, weight, now)

    def quarantine_param(self, param: ModelParam, reason: str) -> None:
        """Move a model_params row aside so it no longer feeds any prior."""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO model_params_quarantine(track_id, compound, name, reason,"
                " value, weight, updated_at, quarantined_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(track_id, compound, name, reason) DO UPDATE SET"
                " value=excluded.value, weight=excluded.weight,"
                " updated_at=excluded.updated_at, quarantined_at=excluded.quarantined_at",
                (
                    param.track_id,
                    param.compound,
                    param.name,
                    reason,
                    param.value,
                    param.weight,
                    param.updated_at,
                    time.time(),
                ),
            )
            self._conn.execute(
                "DELETE FROM model_params WHERE track_id=? AND compound=? AND name=?",
                (param.track_id, param.compound, param.name),
            )

    def quarantined_params(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM model_params_quarantine ORDER BY track_id, compound, name, reason", ()
        )

    def insert_quarantine_if_absent(self, row: Mapping[str, Any]) -> bool:
        with self.transaction():
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO model_params_quarantine(track_id, compound, name, reason,"
                " value, weight, updated_at, quarantined_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["track_id"],
                    row["compound"],
                    row["name"],
                    row["reason"],
                    row["value"],
                    row["weight"],
                    row.get("updated_at"),
                    row.get("quarantined_at"),
                ),
            )
        return cursor.rowcount > 0

    def maintenance_version(self, key: str) -> int:
        row = self._conn.execute("SELECT version FROM maintenance WHERE key=?", (key,)).fetchone()
        return int(row["version"]) if row is not None else 0

    def maintenance_versions(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT key, version FROM maintenance ORDER BY key").fetchall()
        return {str(row["key"]): int(row["version"]) for row in rows}

    def set_maintenance_version(self, key: str, version: int) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO maintenance(key, version, ran_at) VALUES(?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET version=excluded.version,"
                " ran_at=excluded.ran_at",
                (key, version, time.time()),
            )

    def mark_graded(self, uid: int) -> None:
        self.set_maintenance_version(f"graded:{_uid_to_sql(uid)}", 1)

    def learning_stints(self) -> list[dict[str, Any]]:
        """Player stints with their session's track, type and race distance,
        oldest first: the source every stint-derived prior is rebuilt from."""
        return self._rows(
            "SELECT se.track_id, se.session_type, se.total_laps, st.session_uid,"
            " st.compound, st.start_lap, st.end_lap, st.n_valid_laps, st.deg_params"
            " FROM stints st"
            " JOIN sessions se ON se.uid=st.session_uid"
            " WHERE st.car_idx=0 AND COALESCE(se.synthetic, 0)=0"
            " ORDER BY se.started_at, se.uid, st.start_lap",
            (),
        )

    def ungraded_sessions(self) -> list[int]:
        """Sessions with laps but no hindsight outcomes yet."""
        rows = self._conn.execute(
            "SELECT se.uid FROM sessions se WHERE EXISTS"
            " (SELECT 1 FROM laps l WHERE l.session_uid=se.uid)"
            " AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.session_uid=se.uid)"
            " AND NOT EXISTS (SELECT 1 FROM maintenance m WHERE m.key='graded:' || se.uid)"
            " ORDER BY se.started_at, se.uid"
        ).fetchall()
        return [_uid_from_sql(int(r["uid"])) for r in rows]

    def set_session_origin(
        self,
        uid: int,
        *,
        started_at: float,
        recording_path: str,
        calls_mode: str,
        synthetic: bool = False,
        derived_from: str = "",
    ) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE sessions SET started_at=?, recording_path=?, calls_mode=?,"
                " synthetic=MAX(COALESCE(synthetic, 0), ?),"
                " derived_from=CASE WHEN ?<>'' THEN ? ELSE derived_from END WHERE uid=?",
                (
                    started_at,
                    recording_path,
                    calls_mode,
                    int(synthetic),
                    derived_from,
                    derived_from,
                    _uid_to_sql(uid),
                ),
            )

    def mark_ingested(self, uid: int, digest_version: int, path: str) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO ingested(session_uid,path,digest_version,ingested_at)"
                " VALUES(?,?,?,?) ON CONFLICT(session_uid) DO UPDATE SET"
                " path=excluded.path, digest_version=excluded.digest_version,"
                " ingested_at=excluded.ingested_at",
                (_uid_to_sql(uid), path, digest_version, time.time()),
            )

    def is_ingested(self, uid: int, digest_version: int) -> bool:
        row = self._conn.execute(
            "SELECT digest_version FROM ingested WHERE session_uid=?", (_uid_to_sql(uid),)
        ).fetchone()
        return row is not None and int(row["digest_version"]) == digest_version

    def session_has_laps(self, uid: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM laps WHERE session_uid=? LIMIT 1", (_uid_to_sql(uid),)
        ).fetchone()
        return row is not None

    def sessions_for_track(self, track_id: int) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM sessions WHERE track_id=? ORDER BY started_at, uid", (track_id,)
        )
        for row in rows:
            row["uid"] = _uid_from_sql(int(row["uid"]))
        return rows

    def sessions(self, track_id: int | None = None) -> list[dict[str, Any]]:
        if track_id is None:
            rows = self._rows("SELECT * FROM sessions ORDER BY started_at, uid", ())
            for row in rows:
                row["uid"] = _uid_from_sql(int(row["uid"]))
            return rows
        return self.sessions_for_track(track_id)

    def session(self, uid: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE uid=?", (_uid_to_sql(uid),)
        ).fetchone()
        if row is None:
            return None
        session = dict(row)
        session["uid"] = _uid_from_sql(int(session["uid"]))
        return session

    def ingested_uids(self) -> set[int]:
        rows = self._conn.execute("SELECT session_uid FROM ingested").fetchall()
        return {_uid_from_sql(int(r[0])) for r in rows}

    def ingested_recordings(self) -> dict[Path, float]:
        return {
            Path(str(row["path"])).expanduser().resolve(): float(row["ingested_at"])
            for row in self._conn.execute("SELECT path, ingested_at FROM ingested")
            if row["path"] and row["ingested_at"] is not None
        }

    def ingested_count(self, track_id: int | None = None) -> int:
        if track_id is None:
            row = self._conn.execute("SELECT count(*) FROM ingested").fetchone()
        else:
            row = self._conn.execute(
                "SELECT count(*) FROM ingested i JOIN sessions s ON i.session_uid=s.uid"
                " WHERE s.track_id=?",
                (track_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def all_params(self) -> list[ModelParam]:
        rows = self._conn.execute("SELECT * FROM model_params ORDER BY track_id, compound, name")
        return [self._param_row(r) for r in rows.fetchall()]

    def driver_input_counts(self, track_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT di.item_id, di.inputs FROM driver_inputs di"
            " JOIN sessions se ON se.uid=di.session_uid"
            " WHERE se.track_id=? ORDER BY di.item_id",
            (track_id,),
        ).fetchall()
        counts: dict[str, dict[str, Any]] = {}
        for row in rows:
            item_id = str(row["item_id"])
            cases = counts.setdefault(item_id, {"rule_id": item_id, "ack_count": 0, "neg_count": 0})
            try:
                inputs = json.loads(str(row["inputs"] or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(inputs, dict):
                continue
            outcome = inputs.get("case")
            if outcome == "ack":
                cases["ack_count"] += 1
            elif outcome == "neg":
                cases["neg_count"] += 1
        return [counts[key] for key in sorted(counts)]

    def all_grades(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM call_grades ORDER BY graded_at", ())

    def insert_grade_if_absent(self, row: Mapping[str, Any]) -> bool:
        with self.transaction():
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO call_grades(session_uid, call_id, rule_id, grade, note,"
                " graded_at, source) VALUES(?,?,?,?,?,?,?)",
                (
                    _uid_to_sql(int(row["session_uid"])),
                    row["call_id"],
                    row["rule_id"],
                    row["grade"],
                    row.get("note", ""),
                    row.get("graded_at"),
                    row.get("source", "human"),
                ),
            )
        return cursor.rowcount > 0

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
        with self.transaction():
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
        with self.transaction():
            self._conn.execute(
                "INSERT INTO runtime(key, session_uid, session_t, wall_t,"
                " recording_path, lap_num) VALUES('heartbeat',?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " session_uid=excluded.session_uid, session_t=excluded.session_t,"
                " wall_t=excluded.wall_t, recording_path=excluded.recording_path,"
                " lap_num=excluded.lap_num",
                (_uid_to_sql(session_uid), session_t, wall_t, recording_path, lap_num),
            )

    def clear_heartbeat(self) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM runtime WHERE key='heartbeat'")

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
        with self.transaction():
            self._conn.execute(
                "UPDATE sessions SET ended_at=? WHERE uid=?", (ended_at, _uid_to_sql(uid))
            )


def open_configured(settings: Any) -> Database | None:
    """Open the persistence DB from settings; None when disabled."""
    if not settings.persistence.enabled:
        return None
    return Database(Path(settings.persistence.path).expanduser())


SessionUidSource = Callable[[], int]
