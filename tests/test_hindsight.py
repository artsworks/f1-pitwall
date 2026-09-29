"""Hindsight grader, session digest and auto-labelled tune (docs/20 L1)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pitwall.cli import main
from pitwall.digest import build_digest
from pitwall.hindsight import grade_session, linear_deg, stints, stop_cost_s, stop_laps
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database
from pitwall.tune import tune_from_db

UID = 42
MEDIUM, HARD = 17, 18


def _lap(
    n: int, ms: int, compound: int, age: int, *, pitted: bool = False, fuel: float = 1.0
) -> LapSummary:
    return LapSummary(
        n, ms, ms // 3, ms // 3, compound, age, fuel, not pitted, ["pitted"] if pitted else []
    )


def _race(
    db: Database, stop_lap: int, laps: int = 20, *, uid: int = UID, total: int | None = None
) -> None:
    """M (base 90 s, +200 ms/lap) then H (base 90.5 s, +50 ms/lap)."""
    db.upsert_session(uid, track_id=7, session_type=15)
    db.set_session_total_laps(uid, laps if total is None else total)
    age = 0
    compound = MEDIUM
    for n in range(1, laps + 1):
        base, deg = (90_000, 200) if compound == MEDIUM else (90_500, 50)
        pitted = n == stop_lap
        db.insert_lap(
            uid,
            0,
            _lap(n, base + deg * age, compound, age, pitted=pitted, fuel=0.8 if n == laps else 3.0),
        )
        age += 1
        if pitted:
            compound, age = HARD, 0


def _call(db: Database, cid: str, rule: str, lap: int, uid: int = UID, **inputs: object) -> None:
    db.insert_call(
        uid,
        {
            "call_id": cid,
            "rule_id": rule,
            "lap": lap,
            "outcome": "fired",
            "t": float(lap),
            "inputs": inputs,
        },
    )


def test_stop_detection_and_split(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 8)
    laps = db.laps_for(UID)
    assert stop_laps(laps) == [8]
    parts = stints(laps, [8])
    assert [len(p) for p in parts] == [8, 12]


def test_stop_cost_prefers_hindsight_best(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 16)
    laps = db.laps_for(UID)
    parts = stints(laps, stop_laps(laps))
    res = stop_cost_s(parts[0], parts[1], 3)
    assert res is not None
    cost, best = res
    assert cost > 1.5 and best < 16


def test_grades_box_fuel_and_ignored_calls(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 16)
    _call(db, "c1", "box_now", 16, pit_plan="box_now")
    _call(db, "c2", "box_now", 5, pit_plan="box_now")
    _call(db, "c3", "fuel_marginal", 10, fuel_margin_laps=0.6)
    _call(db, "c4", "fuel_spare", 10, fuel_margin_laps=3.0)
    by = {o.call_id: o for o in grade_session(db, UID, {})}
    assert by["c1"].metric == "stop_cost_s" and by["c1"].label == "wrong"
    assert "best in hindsight" in by["c1"].detail
    assert by["c2"].label == "ignored"
    assert by["c3"].label == "good"
    assert by["c4"].label == "wrong"


def test_refired_box_call_graded_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 16)
    for lap in (14, 15, 16):
        _call(db, f"b{lap}", "box_now", lap, pit_plan="box_now")
    by = {o.call_id: o for o in grade_session(db, UID, {})}
    assert by["b14"].label == by["b15"].label == "n/a" and "refired" in by["b14"].detail
    assert by["b16"].metric == "stop_cost_s" and by["b16"].label == "wrong"


def test_neutralised_and_tactical_stops_not_graded_on_deg(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 16)
    _call(db, "sc", "plan_sc_box", 16)
    _call(db, "cheap", "box_now", 16, pit_plan="cheap_stop")
    by = {o.call_id: o for o in grade_session(db, UID, {})}
    assert by["sc"].metric == by["cheap"].metric == "stop_cost_s"
    assert by["sc"].label == by["cheap"].label == "n/a"


def test_unfinished_session_censors_fuel(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 8, laps=12, total=20)
    _call(db, "f", "fuel_marginal", 3, fuel_margin_laps=0.8)
    (o,) = grade_session(db, UID, {})
    assert o.metric == "fuel_margin" and o.label == "censored"


def test_laps_of_pace_uses_age_zero_reference(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    db.upsert_session(UID, track_id=7, session_type=15)
    db.set_session_total_laps(UID, 30)
    for n in range(1, 31):
        db.insert_lap(UID, 0, _lap(n, 90_000 + 100 * (n - 1), MEDIUM, n - 1))
    _call(db, "t", "tyre_life", 10, laps_of_pace=6.0)  # 1500/100 - age 9
    (o,) = grade_session(db, UID, {"tyre_cliff_ms": 1500})
    assert o.metric == "laps_of_pace" and o.label == "good" and o.actual == 6.0


def test_executed_compound_ignores_new_set_on_in_lap(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    db.upsert_session(UID, track_id=7, session_type=15)
    for n in range(1, 13):
        compound, age = (MEDIUM, n - 1) if n < 8 else (HARD, n - 8)
        db.insert_lap(UID, 0, _lap(n, 90_000, compound, age, pitted=n == 8))
    db.insert_plan_event(UID, {"lap": 1, "kind": "set", "to_plan": "A", "sequence": "M-H"})
    (o,) = grade_session(db, UID, {})
    assert o.label == "good", o.detail


def test_plan_followed(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 10)
    db.insert_plan_event(UID, {"lap": 1, "kind": "set", "to_plan": "A", "sequence": "M-H"})
    db.insert_plan_event(UID, {"lap": 3, "kind": "switch", "to_plan": "B", "sequence": "M-H-S"})
    labels = [o.label for o in grade_session(db, UID, {}) if o.metric == "plan_followed"]
    assert labels == ["n/a", "wrong"]  # A superseded by B before any stop


def test_digest_is_idempotent_and_feeds_tune(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    uids = [UID + i for i in range(4)]
    for i, uid in enumerate(uids):
        _race(db, 16, uid=uid)
        _call(db, f"b{i}", "box_now", 16, uid=uid)
    first = build_digest(db, UID, {})
    assert build_digest(db, UID, {}) == first
    assert len(db.outcomes_for_session(UID)) == 1
    assert first["strategy"]["executed"] == "M-H"
    assert first["calls"]["box_now"]["auto_wrong"] == 1
    assert any("Stop cost" in f for f in first["findings"])
    json.dumps(first)
    for uid in uids[1:]:
        build_digest(db, uid, {})

    rows = {r.rule_id: r for r in tune_from_db(db, {})}
    assert rows["box_now"].auto == 4 and rows["box_now"].cooldown_mult > 1.0
    for i, uid in enumerate(uids):
        db.grade_call(uid, f"b{i}", "box_now", "good", "")
    rows = {r.rule_id: r for r in tune_from_db(db, {})}
    assert rows["box_now"].auto == 0 and rows["box_now"].cooldown_mult < 1.0


def test_digest_cli_writes_json(tmp_path: Path, capsys: object) -> None:
    db_path = tmp_path / "h.sqlite"
    db = Database(db_path)
    _race(db, 8)
    db.close()
    out = tmp_path / "digests"
    assert main(["digest", "--db", str(db_path), "--out", str(out)]) == 0
    data = json.loads((out / f"{UID}.json").read_text())
    assert data["session"]["track_id"] == 7 and data["stops"] == [8]


def test_stop_cost_prices_used_second_set_at_its_age(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    db.upsert_session(UID, track_id=7, session_type=15)
    for n in range(1, 31):
        compound, age = (MEDIUM, n - 1) if n <= 15 else (HARD, n - 11)  # used H, age 5
        base, deg = (90_000, 200) if compound == MEDIUM else (90_500, 200)
        db.insert_lap(UID, 0, _lap(n, base + deg * age, compound, age, pitted=n == 15))
    laps = db.laps_for(UID, 0)
    before, after = stints(laps, stop_laps(laps))
    fresh = stop_cost_s(before, [replace(r, tyre_age_laps=r.tyre_age_laps - 5) for r in after], 3)
    used = stop_cost_s(before, after, 3)
    assert fresh is not None and used is not None
    assert used == fresh  # same pace data: only the set's real age, not a reset to 0, prices it


def test_linear_deg_adds_back_fuel_burn(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    db.upsert_session(UID, track_id=7, session_type=15)
    for n in range(1, 11):  # +120 ms/lap deg masked by -130 ms/lap fuel
        db.insert_lap(UID, 0, _lap(n, 90_000 - 10 * n, MEDIUM, n, fuel=20.0 - n))
    laps = db.laps_for(UID, 0)
    raw = linear_deg(laps)
    assert raw is not None and raw[1] == 0.0
    fit = linear_deg(laps, 130.0)
    assert fit is not None and abs(fit[1] - 120.0) < 1e-6


def test_drive_through_is_not_a_stop(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 16)
    db.insert_lap(UID, 0, _lap(21, 95_000, HARD, 5, pitted=True))  # penalty, same set
    laps = db.laps_for(UID, 0)
    assert stop_laps(laps) == [16]


def test_plan_censored_when_session_ends_early(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 99, laps=10, total=20)  # retired on lap 10, never stopped
    db.insert_plan_event(UID, {"lap": 1, "kind": "set", "to_plan": "A", "sequence": "M-H"})
    (o,) = grade_session(db, UID, {})
    assert o.label == "censored"


def test_unplanned_extra_stop_is_not_following_plan(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 10)
    db.insert_plan_event(UID, {"lap": 1, "kind": "set", "to_plan": "A", "sequence": "M"})
    (o,) = grade_session(db, UID, {})
    assert o.label == "wrong", o.detail


def test_single_off_lap_is_not_the_cliff(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    db.upsert_session(UID, track_id=7, session_type=15)
    db.set_session_total_laps(UID, 30)
    for n in range(1, 31):
        ms = 90_000 + 100 * (n - 1) + (1_600 if n == 14 else 0)
        db.insert_lap(UID, 0, _lap(n, ms, MEDIUM, n - 1))
    _call(db, "t", "tyre_life", 10, laps_of_pace=6.0)
    (o,) = grade_session(db, UID, {"tyre_cliff_ms": 1500})
    assert o.label == "good" and o.actual == 6.0
