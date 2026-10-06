"""Named strategy plans end to end: synthetic race replays through the engine,
plan radio rules, decision-log inputs and SQLite plan_events / calls columns."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from pitwall.audio.dispatcher import Call
from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.store.db import MIGRATIONS, Database

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream

pytestmark = pytest.mark.slow

Run = tuple[list[Call], list[dict], list[dict], list[dict]]


def _replay(tmp: Path, spec: RaceSpec) -> Run:
    path = write_packet_stream(tmp / "race.f1bin", race_stream(spec))
    log = tmp / "decisions.jsonl"
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_path=log)
    _, calls = asyncio.run(run_replay(path, engine, None))
    engine.dispatcher.log.flush()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line]
    uid = engine.state.session_uid
    assert uid is not None
    return calls, rows, db.plan_events_for_session(uid), db.calls_for_session(uid)


SCENARIOS = {
    "sc": RaceSpec(laps=10, sc_laps=(5, 6), wear_pct_per_lap=10),
    "player_pit": RaceSpec(laps=12, player_pit_lap=6),
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Run]:
    return {name: _replay(tmp_path_factory.mktemp(name), spec) for name, spec in SCENARIOS.items()}


def test_plan_a_announced_on_lap_1(runs: dict[str, Run]) -> None:
    calls, rows, events, _ = runs["player_pit"]
    first = next(c for c in calls if c.rule_id.startswith("plan_"))
    assert first.rule_id == "plan_announce" and first.lap == 1
    assert "Plan A" in first.text and "Plan B" in first.text
    assert events[0]["kind"] == "set" and events[0]["to_plan"] == "A"
    plans = json.loads(events[0]["plans"])
    assert [p["id"] for p in plans] == ["A", "B", "C"]
    assert plans[0]["delta_s"] == 0.0 and plans[1]["delta_s"] > 0
    # log mirror of the plan event
    assert any(r["outcome"] == "plan" and r["kind"] == "set" for r in rows)


def test_window_open_and_invalidated_after_off_plan_stop(runs: dict[str, Run]) -> None:
    calls, _, events, _ = runs["player_pit"]
    ids = [c.rule_id for c in calls]
    assert "plan_window_open" in ids
    # synthetic stop fits another medium: M-S is no longer Plan A's remainder
    inv = next(c for c in calls if c.rule_id == "plan_invalid")
    assert "off Plan A" in inv.text
    sw = next(e for e in events if e["kind"] == "switch")
    assert (sw["from_plan"], sw["reason"]) == ("A", "invalid")
    assert inv.lap == sw["lap"]


def test_sc_switches_to_plan_c_with_box_call(runs: dict[str, Run]) -> None:
    calls, rows, events, _ = runs["sc"]
    sw = next(e for e in events if e["kind"] == "switch")
    assert (sw["from_plan"], sw["to_plan"], sw["reason"]) == ("A", "C", "sc")
    box = next(c for c in calls if c.rule_id == "box_now")
    assert box.lap == sw["lap"] and "Plan C" in box.text
    fired = next(r for r in rows if r["rule_id"] == "box_now" and r["outcome"] == "fired")
    assert fired["active_plan"] == "C" and fired["inputs"]["active_plan"] == "C"
    # plan_sc_box is the fallback and shares the pit cooldown group
    assert not any(c.rule_id == "plan_sc_box" for c in calls)
    # the stop isn't taken in the fixture: back to A once the SC is in
    back = [e for e in events if e["kind"] == "switch"][1]
    assert (back["from_plan"], back["reason"]) == ("C", "invalid")
    assert any(c.rule_id == "plan_invalid" and "off Plan C" in c.text for c in calls)


def test_calls_rows_carry_active_plan(runs: dict[str, Run]) -> None:
    _, _, _, rows = runs["sc"]
    fired = [r for r in rows if r["outcome"] == "fired"]
    planned = [r for r in fired if r["active_plan"]]
    assert planned and all(r["on_plan"] in (0, 1) for r in planned)
    assert {r["active_plan"] for r in planned} >= {"A", "C"}


def test_migration_adds_plan_columns_to_existing_db(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version=2")
    conn.execute("INSERT INTO calls(session_uid, rule_id, outcome) VALUES(3, 'box_now', 'fired')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # noqa: SLF001
    old = db.calls_for_session(3)
    assert len(old) == 1 and old[0]["active_plan"] is None and old[0]["on_plan"] is None
    db.insert_call(
        3, {"outcome": "fired", "rule_id": "plan_status", "active_plan": "B", "on_plan": False}
    )
    db.insert_plan_event(
        3,
        {
            "t": 1.0,
            "lap": 4,
            "kind": "switch",
            "from_plan": "A",
            "to_plan": "B",
            "reason": "pace",
            "delta_s": 6.2,
            "sequence": "M-S-S",
            "plans": [{"id": "A"}],
        },
    )
    new = db.calls_for_session(3)[-1]
    assert (new["active_plan"], new["on_plan"]) == ("B", 0)
    ev = db.plan_events_for_session(3)
    assert len(ev) == 1 and ev[0]["to_plan"] == "B" and json.loads(ev[0]["plans"]) == [{"id": "A"}]
