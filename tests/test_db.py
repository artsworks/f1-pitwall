from __future__ import annotations

import sqlite3

from pitwall.derive import derived_uid
from pitwall.model.deg import DegFit
from pitwall.state.lap import LapAccumulator, LapSummary
from pitwall.store.db import MIGRATIONS, Database


def test_fresh_db_is_migrated(tmp_path) -> None:
    db = Database(tmp_path / "x.sqlite")
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]  # noqa: SLF001
    assert version == len(MIGRATIONS)
    # re-open: no-op
    db2 = Database(tmp_path / "x.sqlite")
    assert db2._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # noqa: SLF001


def test_press_grades_yield_to_human_and_preserve_unsigned_uid() -> None:
    db = Database(":memory:")
    uid = (1 << 63) + 42

    db.grade_call(uid, "call-1", "rule", "good", source="press")
    grade = db.grades_for_session(uid)[0]
    assert grade["source"] == "press" and grade["grade"] == "good"

    db.grade_call(uid, "call-1", "rule", "noise", source="press")
    assert db.grades_for_session(uid)[0]["grade"] == "noise"

    db.grade_call(uid, "call-1", "rule", "wrong")
    db.grade_call(uid, "call-1", "rule", "good", source="press")
    grade = db.grades_for_session(uid)[0]
    assert grade["grade"] == "wrong" and grade["source"] == "human"

    db.grade_call(uid, "call-1", "rule", "good")
    assert db.grades_for_session(uid)[0]["grade"] == "good"


def test_bookmark_persists_kind_and_context() -> None:
    db = Database(":memory:")
    uid = 21
    db.insert_call(
        uid,
        {
            "t": 2.0,
            "lap": 3,
            "outcome": "bookmark",
            "kind": "tap",
            "context": {"lap_num": 3, "fuel_remaining_laps": 4.2357},
        },
    )

    bookmark = db.bookmarks_for_session(uid)[0]
    assert bookmark["kind"] == "tap"
    assert bookmark["context"] == {"lap_num": 3, "fuel_remaining_laps": 4.2357}


def test_insert_call_stores_press_grade() -> None:
    db = Database(":memory:")
    db.insert_call(
        22,
        {
            "outcome": "ack",
            "call_id": "call-1",
            "rule_id": "tyre_temp",
            "grade": "good",
            "grade_source": "press",
        },
    )

    grade = db.grades_for_session(22)[0]
    assert grade["grade"] == "good" and grade["source"] == "press"


def test_insert_call_skips_press_grades_for_synthetic_sessions() -> None:
    db = Database(":memory:")
    real_uid = 22
    synthetic_uid = derived_uid(real_uid, ["wear_scale:3"])
    db.upsert_session(real_uid)
    db.upsert_session(synthetic_uid)

    for uid in (real_uid, synthetic_uid):
        db.insert_call(
            uid,
            {
                "outcome": "ack",
                "call_id": "call-1",
                "rule_id": "tyre_temp",
                "grade": "good",
                "grade_source": "press",
            },
        )

    grades = db.grades_for_session(real_uid)
    assert len(grades) == 1 and grades[0]["source"] == "press"
    assert db.grades_for_session(synthetic_uid) == []
    assert db.calls_for_session(synthetic_uid)[0]["outcome"] == "ack"


def test_incremental_migration() -> None:
    import pitwall.store.db as dbmod

    db = Database(":memory:")  # fully migrated -> version == len(MIGRATIONS)
    try:
        dbmod.MIGRATIONS.append("CREATE TABLE _tmp_test(id INT)")
        db.migrate()  # applies only the appended script
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == len(  # noqa: SLF001
            dbmod.MIGRATIONS
        )
        db._conn.execute("SELECT id FROM _tmp_test")
        db._conn.execute("SELECT uid FROM sessions")  # earlier tables intact
    finally:
        dbmod.MIGRATIONS.pop()


def test_setup_advisor_migration_from_previous_version(tmp_path) -> None:
    path = tmp_path / "previous.sqlite"
    conn = sqlite3.connect(path)
    for version, migration in enumerate(MIGRATIONS[:-1], start=1):
        conn.executescript(migration)
        conn.execute(f"PRAGMA user_version={version}")
    conn.close()

    db = Database(path)

    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # noqa: SLF001
    tables = {
        row["name"]
        for row in db._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"setup_states", "setup_changes", "setup_recs"} <= tables
    columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(laps)")  # noqa: SLF001
    }
    assert {
        "setup_state_id",
        "traction_exits",
        "slip_balance_deg",
        "slip_samples",
        "lockups_front",
        "lockups_rear",
        "snaps_entry",
        "snaps_exit",
    } <= columns


def test_setup_state_change_lap_and_stint_round_trip() -> None:
    db = Database(":memory:")
    uid = 83
    db.upsert_session(uid, track_id=7, parc_ferme=2)
    state_id = db.setup_state_id("sha1:setup", {"brake_bias": 56.0})
    assert db.setup_state_id("sha1:setup", {"brake_bias": 56.0}) == state_id
    assert db.setup_state_fields(state_id) == {"brake_bias": 56.0}

    db.insert_setup_change(uid, 1, 4.5, None, state_id)
    db.insert_setup_change(uid, 1, 4.5, None, state_id)
    changes = db.setup_changes_for_session(uid)
    assert len(changes) == 1
    assert changes[0]["to_state"] == state_id and changes[0]["from_state"] is None

    lap = LapSummary(
        lap_num=1,
        lap_time_ms=91_234,
        sector1_ms=30_000,
        sector2_ms=31_000,
        compound=16,
        tyre_age_laps=1,
        fuel_remaining_laps_at_end=14.5,
        valid=True,
        traction_exits=2,
        lockups_front=1,
        lockups_rear=3,
        snaps_entry=1,
        snaps_exit=2,
        slip_balance_deg=2.5,
        slip_samples=14,
    )
    db.insert_lap(uid, 0, lap, setup_state_id=state_id)
    stored_lap = db.laps_for(uid)[0]
    assert stored_lap.setup_state_id == state_id
    assert stored_lap.traction_exits == 2
    assert stored_lap.lockups_front == 1 and stored_lap.lockups_rear == 3
    assert stored_lap.snaps_entry == 1 and stored_lap.snaps_exit == 2
    assert stored_lap.slip_balance_deg == 2.5 and stored_lap.slip_samples == 14

    db.upsert_stint(
        uid,
        0,
        16,
        1,
        1,
        DegFit(90_000.0, 100.0, 30.0, 1, 100.0, 0.9, "fit"),
        setup_state_id=state_id,
    )
    assert db.stints_for_session(uid)[0].setup_state_id == state_id
    db.set_session_parc_ferme(uid, 3)
    assert db.session_row(uid)["parc_ferme"] == 3


def test_call_grade_bookmark_round_trip() -> None:
    db = Database(":memory:")
    db.upsert_session(42, track_id=3, session_type=5)
    rec = {
        "outcome": "fired",
        "call_id": "c-1",
        "t": 12.5,
        "session_time": 12.5,
        "lap": 4,
        "lap_distance": 123.4,
        "rule_id": "release_go",
        "priority": 2,
        "text": "go now",
        "inputs": {"release_clean": True},
        "config_hash": "abc",
        "mindset": "default",
    }
    db.insert_call(42, rec)
    db.insert_call(42, {"outcome": "paused", "t": 13.0})  # not persisted
    db.insert_call(  # bookmark -> bookmarks table
        42, {"outcome": "bookmark", "t": 14.0, "lap": 4, "session_time": 14.0}
    )
    calls = db.calls_for_session(42)
    assert len(calls) == 1 and calls[0]["rule_id"] == "release_go"
    bm = db.bookmarks_for_session(42)
    assert len(bm) == 1

    db.grade_call(42, "c-1", "release_go", "good")
    db.grade_call(42, "c-1", "release_go", "noise", "too chatty")  # upsert
    grades = db.grades_for_session(42)
    assert len(grades) == 1 and grades[0]["grade"] == "noise"
    assert db.latest_session_uid() == 42


def test_insert_lap() -> None:
    db = Database(":memory:")
    db.upsert_session(7)
    lap = LapSummary(
        lap_num=3,
        lap_time_ms=91_234,
        sector1_ms=30_000,
        sector2_ms=31_000,
        compound=16,
        tyre_age_laps=2,
        fuel_remaining_laps_at_end=14.5,
        valid=False,
        invalid_reasons=["lap_invalid"],
    )
    db.insert_lap(7, 0, lap)
    rows = db._rows("SELECT * FROM laps WHERE session_uid=?", (7,))  # noqa: SLF001
    assert len(rows) == 1
    assert rows[0]["lap_time_ms"] == 91_234 and rows[0]["valid"] == 0


def test_track_minutes_deduplicates_player_laps_and_preserves_uint64_uid() -> None:
    db = Database(":memory:")
    uid = 0xACBF76B8C45ADE98
    db.upsert_session(7)
    db.upsert_session(uid)

    def lap(lap_num: int, lap_time_ms: int) -> LapSummary:
        return LapSummary(lap_num, lap_time_ms, 30_000, 31_000, 16, 1, 4.0, True)

    db.insert_lap(7, 0, lap(1, 90_000))
    db.insert_lap(7, 0, lap(1, 100_000))
    db.insert_lap(uid, 0, lap(1, 60_000))
    db.insert_lap(uid, 1, lap(2, 80_000))
    db.insert_lap(7, 0, lap(2, 0))

    assert db.track_minutes() == {"minutes": 2.7, "laps": 2, "sessions": 2}


def test_uint64_session_uid_round_trip() -> None:
    uid = 0xACBF76B8C45ADE98
    db = Database(":memory:")
    db.upsert_session(uid, track_id=0, session_type=5)
    db.insert_call(uid, {"outcome": "fired", "call_id": "c-1", "t": 1.0, "rule_id": "r"})
    db.insert_call(uid, {"outcome": "bookmark", "t": 2.0})
    db.grade_call(uid, "c-1", "r", "good")
    assert db.latest_session_uid() == uid
    assert db.calls_for_session(uid)[0]["session_uid"] == uid
    assert db.grades_for_session(uid)[0]["session_uid"] == uid
    assert db.bookmarks_for_session(uid)[0]["session_uid"] == uid


def test_call_inputs_with_dataclass_values_are_stored() -> None:
    from pitwall.state.session import Damage

    db = Database(":memory:")
    db.insert_call(1, {"outcome": "fired", "call_id": "c-1", "inputs": {"damage": Damage(7)}})
    assert "front_left_wing" in db.calls_for_session(1)[0]["inputs"]


def test_m4_session_ingest_and_lap_temperature_persistence() -> None:
    db = Database(":memory:")
    uid = 0xF1264001
    db.upsert_session(
        uid,
        track_id=7,
        session_type=15,
        started_at=100.0,
        weekend_link=27,
        calls_mode="on",
    )
    db.set_session_origin(
        uid,
        started_at=50.0,
        recording_path="race.f1bin",
        calls_mode="off",
    )
    db.insert_lap(
        uid,
        0,
        LapSummary(
            lap_num=2,
            lap_time_ms=90_000,
            sector1_ms=30_000,
            sector2_ms=30_000,
            compound=17,
            tyre_age_laps=1,
            fuel_remaining_laps_at_end=10.0,
            valid=True,
            tyre_inner_c=92.5,
            tyre_inner_front_c=95.0,
            tyre_inner_rear_c=90.0,
            tyre_surface_c=105.25,
            wear_front_pct=12.0,
            wear_rear_pct=10.0,
        ),
    )

    row = db.session_row(uid)
    lap = db.laps_for(uid)[0]
    assert row is not None
    assert row["started_at"] == 50.0
    assert row["recording_path"] == "race.f1bin"
    assert row["weekend_link"] == 27 and row["calls_mode"] == "off"
    assert lap.tyre_inner_c == 92.5 and lap.tyre_surface_c == 105.25
    assert (lap.wear_front_pct, lap.wear_rear_pct) == (12.0, 10.0)
    assert (lap.tyre_inner_front_c, lap.tyre_inner_rear_c) == (95.0, 90.0)
    assert db.session_has_laps(uid)

    db.mark_ingested(uid, 3, "race.f1bin")
    assert db.is_ingested(uid, 3)
    assert not db.is_ingested(uid, 2)
    assert db.ingested_count(7) == 1
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # noqa: SLF001


def test_lap_temperature_accumulator_averages_samples_and_corners() -> None:
    accumulator = LapAccumulator()
    accumulator.update(
        current_lap_num=1,
        last_lap_time_ms=0,
        sector1_ms=0,
        sector2_ms=0,
        pit_status=0,
        driver_status=4,
        current_lap_invalid=0,
        safety_car_status=0,
        compound=17,
        tyre_age_laps=0,
        fuel_remaining_laps=10.0,
    )
    accumulator.note_tyre_temperatures((80, 90, 100, 110), (100, 110, 120, 130))
    accumulator.note_tyre_temperatures((100, 110, 120, 130), (120, 130, 140, 150))

    lap = accumulator.update(
        current_lap_num=2,
        last_lap_time_ms=90_000,
        sector1_ms=30_000,
        sector2_ms=30_000,
        pit_status=0,
        driver_status=4,
        current_lap_invalid=0,
        safety_car_status=0,
        compound=17,
        tyre_age_laps=1,
        fuel_remaining_laps=9.0,
    )

    assert lap is not None
    assert lap.tyre_inner_c == 105.0
    assert lap.tyre_surface_c == 125.0
    assert lap.tyre_inner_front_c == 115.0
    assert lap.tyre_inner_rear_c == 95.0
