"""Hindsight grader, session digest and auto-labelled tune (docs/20 L1)."""

from __future__ import annotations

import json
from pathlib import Path

from pitwall.cli import main
from pitwall.digest import build_digest
from pitwall.hindsight import grade_session, stints, stop_cost_s, stop_laps
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


def _race(db: Database, stop_lap: int, laps: int = 20) -> None:
    """M (base 90 s, +200 ms/lap) then H (base 90.5 s, +50 ms/lap)."""
    db.upsert_session(UID, track_id=7, session_type=15)
    age = 0
    compound = MEDIUM
    for n in range(1, laps + 1):
        base, deg = (90_000, 200) if compound == MEDIUM else (90_500, 50)
        pitted = n == stop_lap
        db.insert_lap(
            UID,
            0,
            _lap(n, base + deg * age, compound, age, pitted=pitted, fuel=0.8 if n == laps else 3.0),
        )
        age += 1
        if pitted:
            compound, age = HARD, 0


def _call(db: Database, cid: str, rule: str, lap: int, **inputs: object) -> None:
    db.insert_call(
        UID,
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


def test_plan_followed(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 10)
    db.insert_plan_event(UID, {"lap": 1, "kind": "set", "to_plan": "A", "sequence": "M-H"})
    db.insert_plan_event(UID, {"lap": 3, "kind": "switch", "to_plan": "B", "sequence": "M-H-S"})
    labels = [o.label for o in grade_session(db, UID, {}) if o.metric == "plan_followed"]
    assert labels == ["good", "wrong"]


def test_digest_is_idempotent_and_feeds_tune(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.sqlite")
    _race(db, 16)
    for i in range(4):
        _call(db, f"b{i}", "box_now", 16)
    first = build_digest(db, UID, {})
    assert build_digest(db, UID, {}) == first
    assert len(db.outcomes_for_session(UID)) == 4
    assert first["strategy"]["executed"] == "M-H"
    assert first["calls"]["box_now"]["auto_wrong"] == 4
    assert any("Stop cost" in f for f in first["findings"])
    json.dumps(first)

    rows = {r.rule_id: r for r in tune_from_db(db, {})}
    assert rows["box_now"].auto == 4 and rows["box_now"].cooldown_mult > 1.0
    for i in range(4):
        db.grade_call(UID, f"b{i}", "box_now", "good", "")
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
