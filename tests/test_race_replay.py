"""Replay tests for the race rules (config/defaults/rules/race.yaml) and the
M3 exit criterion on synthetic 25% / 100% races (docs/18)."""

from __future__ import annotations

import asyncio
import collections
import json
from pathlib import Path

import pytest

from pitwall.audio.dispatcher import Call
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream


def _replay(tmp: Path, spec: RaceSpec) -> tuple[list[Call], list[dict]]:
    path = write_packet_stream(tmp / "race.f1bin", race_stream(spec))
    log = tmp / "decisions.jsonl"
    engine = build_engine(
        clock=VirtualClock(), sinks=[], db=Database(":memory:"), decision_log_path=log
    )
    _, calls = asyncio.run(run_replay(path, engine, None))
    engine.dispatcher.log.flush()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line]
    return calls, rows


def _ids(calls: list[Call]) -> set[str]:
    return {c.rule_id for c in calls}


SCENARIOS = {
    "base": RaceSpec(laps=8),
    "deg": RaceSpec(laps=10, deg_ms=600, wear_pct_per_lap=8),
    "sc": RaceSpec(laps=10, sc_laps=(5, 6), wear_pct_per_lap=10),
    "penalty": RaceSpec(laps=6, penalty_lap=3),
    "blue": RaceSpec(laps=6, blue_flag_lap=3),
    "rain": RaceSpec(laps=8, rain_lap=3),
    "hot": RaceSpec(laps=6, hot_tyres_lap=3),
    "rival_pit": RaceSpec(laps=10, rival_pit_lap=5),
    "player_pit": RaceSpec(laps=12, player_pit_lap=6),
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[list[Call], list[dict]]]:
    return {name: _replay(tmp_path_factory.mktemp(name), spec) for name, spec in SCENARIOS.items()}


EVENT_RULES = {
    "box_now",
    "penalty",
    "blue_flag",
    "weather_to_inter",
    "tyre_overheat",
    "rival_ahead_pitted",
    "sc_deployed",
    "pit_exit_traffic_race",
}


def test_control_race_is_quiet(runs) -> None:
    calls, _ = runs["base"]
    assert not (_ids(calls) & EVENT_RULES)
    assert "fuel_spare" in _ids(calls)


def test_fuel_spare_carries_budget_inputs(runs) -> None:
    _, rows = runs["base"]
    row = next(r for r in rows if r["rule_id"] == "fuel_spare" and r["outcome"] == "fired")
    assert row["inputs"]["fuel_margin_laps"] > 1.0
    assert row["mindset"] == "balanced"


def test_energy_budget_call(runs) -> None:
    assert "energy_under" in _ids(runs["base"][0])


def test_box_now_when_tyres_go(runs) -> None:
    calls, rows = runs["deg"]
    box = [c for c in calls if c.rule_id == "box_now"]
    assert len(box) == 1
    row = next(r for r in rows if r["rule_id"] == "box_now" and r["outcome"] == "fired")
    inputs = row["inputs"]
    for key in (
        "pit_plan",
        "pit_plan_confidence",
        "pit_plan_risk",
        "pit_plan_gain_s",
        "laps_of_pace",
    ):
        assert key in inputs
    assert inputs["pit_plan"] == "box_now"


def test_sc_cheap_stop(runs) -> None:
    calls, rows = runs["sc"]
    assert "sc_deployed" in _ids(calls)
    box = next(c for c in calls if c.rule_id == "box_now")
    assert box.lap in (5, 6)
    assert "safety car" in box.text
    row = next(r for r in rows if r["rule_id"] == "box_now" and r["outcome"] == "fired")
    assert row["inputs"]["pit_plan"] == "cheap_stop"


def test_penalty_call(runs) -> None:
    calls, _ = runs["penalty"]
    pen = next(c for c in calls if c.rule_id == "penalty")
    assert pen.lap == 3
    assert "5 seconds" in pen.text


def test_blue_flag_call(runs) -> None:
    calls, _ = runs["blue"]
    assert next(c for c in calls if c.rule_id == "blue_flag").lap == 3


def test_weather_crossover_call(runs) -> None:
    calls, _ = runs["rain"]
    call = next(c for c in calls if c.rule_id == "weather_to_inter")
    assert "Inters" in call.text


def test_tyre_overheat_call(runs) -> None:
    calls, _ = runs["hot"]
    assert next(c for c in calls if c.rule_id == "tyre_overheat").lap >= 3


def test_rival_pit_call(runs) -> None:
    calls, _ = runs["rival_pit"]
    call = next(c for c in calls if c.rule_id == "rival_ahead_pitted")
    assert "NORRIS" in call.text


def test_pit_exit_traffic_call(runs) -> None:
    calls, _ = runs["player_pit"]
    call = next(c for c in calls if c.rule_id == "pit_exit_traffic_race")
    assert "LECLERC" in call.text


def _budget_ok(calls: list[Call]) -> None:
    budget = ConfigStore().current().policy.calls_per_lap
    assert budget is not None
    per_lap = collections.Counter(c.lap for c in calls if c.priority != 1)
    assert max(per_lap.values(), default=0) <= budget


@pytest.mark.parametrize("fraction", [0.25, 1.0])
def test_exit_criterion_race(tmp_path: Path, fraction: float) -> None:
    """25% / 100% of a 52-lap race: one defensible stop, calls inside budget."""
    laps = round(52 * fraction)
    spec = RaceSpec(
        laps=laps,
        deg_ms=90 if fraction == 1.0 else 250,
        wear_pct_per_lap=2.0,
        fuel_kg=laps * 1.7 + 3.0,
    )
    calls, rows = _replay(tmp_path, spec)
    _budget_ok(calls)
    box = [r for r in rows if r["rule_id"] == "box_now" and r["outcome"] == "fired"]
    assert box, "no pit call"
    first = box[0]["inputs"]
    assert first["pit_plan_confidence"] >= 0.7 or first["laps_of_pace"] < 1
    assert first["laps_remaining"] > 2
