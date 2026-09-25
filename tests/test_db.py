from __future__ import annotations

from pitwall.state.lap import LapSummary
from pitwall.store.db import MIGRATIONS, Database


def test_fresh_db_is_migrated(tmp_path) -> None:
    db = Database(tmp_path / "x.sqlite")
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]  # noqa: SLF001
    assert version == len(MIGRATIONS)
    # re-open: no-op
    db2 = Database(tmp_path / "x.sqlite")
    assert db2._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # noqa: SLF001


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
