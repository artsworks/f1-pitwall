"""M3 database layer: migration 2, new row APIs, model_params folding."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pitwall.state.lap import LapSummary
from pitwall.store.db import MIGRATIONS, Database


def _lap(lap_num: int = 1, **kw: object) -> LapSummary:
    base = dict(
        lap_num=lap_num,
        lap_time_ms=91_000 + lap_num * 100,
        sector1_ms=30_000,
        sector2_ms=31_000,
        compound=18,
        tyre_age_laps=lap_num,
        fuel_remaining_laps_at_end=30.0 - lap_num,
        valid=True,
        wear_pct=12.5,
        fuel_kg=50.0 - lap_num,
        ers_deployed_j=200_000.0,
        sc_status=1,
        weather=2,
    )
    base.update(kw)
    return LapSummary(**base)  # type: ignore[arg-type]


def _v1_db(path: Path) -> None:
    """A database at migration 1 with one row in each pre-M3 table."""
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0])
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO sessions(uid, track_id) VALUES(11, 7)")
    conn.execute(
        "INSERT INTO laps(session_uid, car_idx, lap_num, lap_time_ms, valid)"
        " VALUES(11, 0, 1, 90000, 1)"
    )
    conn.execute(
        "INSERT INTO stints(session_uid, car_idx, compound, start_lap, end_lap)"
        " VALUES(11, 0, 18, 1, 9)"
    )
    conn.execute("INSERT INTO pit_events(session_uid, lap_num, loss_ms) VALUES(11, 4, 21000)")
    conn.commit()
    conn.close()


def test_migration_1_to_2_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    _v1_db(path)
    db = Database(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # noqa: SLF001
    laps = db.laps_for(11)
    assert len(laps) == 1 and laps[0].lap_time_ms == 90_000
    assert laps[0].wear_pct == 0.0  # migrated NULL reads as 0
    stints = db.stints_for_track(7, 18)
    assert len(stints) == 1 and stints[0].end_lap == 9
    events = db.pit_events_for_session(11)
    assert len(events) == 1 and events[0].loss_ms == 21_000


def test_new_apis_round_trip() -> None:
    db = Database(":memory:")
    db.upsert_session(5, track_id=7, session_type=15, game_mode=3)
    db.insert_lap(5, 0, _lap(1))
    db.insert_lap(5, 1, _lap(1))
    db.insert_lap(5, 0, _lap(2))
    laps = db.laps_for(5, 0)
    assert [r.lap_num for r in laps] == [1, 2]
    assert laps[0].wear_pct == 12.5 and laps[0].fuel_kg == 49.0
    assert laps[0].ers_deployed_j == 200_000.0 and laps[0].sc_status == 1
    assert len(db.laps_for(5, 1)) == 1

    row = db._conn.execute("SELECT game_mode FROM sessions WHERE uid=5").fetchone()  # noqa: SLF001
    assert row[0] == 3

    from pitwall.model.deg import DegFit

    fit = DegFit(90_000.0, 80.0, 30.0, 6, 120.0, 0.8, "fit")
    db.upsert_stint(5, 0, 18, 1, 6, fit)
    db.upsert_stint(5, 0, 18, 1, 8, fit)  # same stint, extended
    stints = db.stints_for_track(7, 18)
    assert len(stints) == 1 and stints[0].end_lap == 8
    assert stints[0].n_valid_laps == 6 and stints[0].deg_ms_per_lap == 80.0

    db.insert_pit_event(5, 0, 9, 21_500, 0, 19_000, 110_000, 96_000, 90_000)
    ev = db.pit_events_for_track(7, 0)
    assert len(ev) == 1 and ev[0].lane_ms == 19_000 and ev[0].car_idx == 0
    assert db.pit_events_for_track(7, 1) == []

    db.record_ab(recording="rec", a_dir="a", b_dir="b", rule_id="r", only_a=1, both=2)
    assert db._conn.execute("SELECT COUNT(*) FROM ab_results").fetchone()[0] == 1  # noqa: SLF001


def test_fold_param_weighted_mean_and_cap() -> None:
    db = Database(":memory:")
    assert db.get_param(7, 0, "pit_loss_green_ms") is None
    p = db.fold_param(7, 0, "pit_loss_green_ms", 20_000.0, weight=1.0)
    assert p.value == 20_000.0 and p.weight == 1.0
    p = db.fold_param(7, 0, "pit_loss_green_ms", 22_000.0, weight=1.0)
    assert p.value == 21_000.0 and p.weight == 2.0
    # weight capped
    p = db.fold_param(7, 0, "deg_ms_per_lap", 100.0, weight=100.0, param_weight_cap=50)
    assert p.weight == 50.0
    names = {(p.compound, p.name) for p in db.params_for_track(7)}
    assert names == {(0, "pit_loss_green_ms"), (0, "deg_ms_per_lap")}


def test_heartbeat_round_trip() -> None:
    db = Database(":memory:")
    assert db.read_heartbeat() is None
    db.write_heartbeat(123, 456.0, 789.0, "rec.f1bin", 9)
    hb = db.read_heartbeat()
    assert hb is not None
    assert hb.session_uid == 123 and hb.session_t == 456.0
    assert hb.wall_t == 789.0 and hb.recording_path == "rec.f1bin" and hb.lap_num == 9
    db.write_heartbeat(123, 500.0, 800.0, "rec.f1bin", 10)
    assert db.read_heartbeat().session_t == 500.0  # type: ignore[union-attr]


def test_end_session() -> None:
    db = Database(":memory:")
    db.upsert_session(9, track_id=3)
    db.end_session(9, 1234.5)
    row = db._conn.execute("SELECT ended_at FROM sessions WHERE uid=9").fetchone()  # noqa: SLF001
    assert row[0] == 1234.5
