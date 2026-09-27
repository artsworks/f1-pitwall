"""Battle state, episodes, pass model and racecraft calls (docs/20 L3)."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine
from pitwall.server.app import strategy_payload
from pitwall.state.session import Snapshot
from pitwall.store.db import Database
from pitwall.strategy.battle import (
    ATTACKING,
    CATCHING,
    DEFENDING,
    FREE,
    MANAGING,
    PASS_COMPOUND,
    PASS_DRS,
    THREAT,
    BattleInputs,
    BattleRates,
    BattleTracker,
    Episode,
    classify,
    shrink,
)

from .race_synth import RaceSpec
from .test_race_replay import _ids, _replay

TH: dict[str, object] = {}


def _inp(**kw: object) -> BattleInputs:
    base: dict[str, object] = {
        "now": 0.0,
        "lap_num": 10,
        "position": 5,
        "laps_remaining": 20,
        "ahead_idx": 1,
        "behind_idx": 2,
        "gap_ahead_s": 8.0,
        "gap_behind_s": 8.0,
        "trend_ahead_s": 0.0,
        "trend_behind_s": 0.0,
        "own_pace_ms": 90_000,
        "ahead_pace_ms": 90_000,
        "behind_pace_ms": 90_000,
        "own_age": 10,
        "ahead_age": 10,
        "behind_age": 10,
        "drs_available": False,
        "attack_gap_s": 1.0,
    }
    base.update(kw)
    return BattleInputs(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "mode"),
    [
        ({}, FREE),
        ({"gap_behind_s": 0.8, "gap_ahead_s": 0.5}, DEFENDING),
        ({"gap_ahead_s": 0.9}, ATTACKING),
        ({"gap_ahead_s": 3.0, "ahead_pace_ms": 90_400}, CATCHING),
        ({"gap_ahead_s": 3.0, "trend_ahead_s": 0.5}, CATCHING),
        ({"gap_behind_s": 2.5, "behind_pace_ms": 89_500}, THREAT),
        ({"gap_ahead_s": 3.0, "gap_behind_s": 2.5}, MANAGING),
        # closing too slowly to reach him before the flag: manage, don't push
        ({"gap_ahead_s": 4.5, "ahead_pace_ms": 90_300, "laps_remaining": 5}, MANAGING),
        ({"gap_ahead_s": math.inf, "ahead_idx": -1}, FREE),
    ],
)
def test_classify(kw: dict[str, object], mode: str) -> None:
    assert classify(_inp(**kw), TH) == mode


def test_attack_hysteresis() -> None:
    inp = _inp(gap_ahead_s=1.2)
    assert classify(inp, TH) == MANAGING
    assert classify(inp, TH, prev=ATTACKING) == ATTACKING


def test_tracker_catch_laps_and_offsets() -> None:
    b = BattleTracker().update(
        _inp(gap_ahead_s=3.0, ahead_pace_ms=90_500, ahead_age=18), TH, BattleRates()
    )
    assert b.mode == CATCHING
    assert b.closing_ahead_s == 0.5
    assert b.catch_laps == 4.0
    assert b.tyre_offset_ahead == 8
    assert b.pass_prob == BattleRates().pass_nodrs


def test_attack_episode_passed_and_encouragement_window() -> None:
    tr = BattleTracker()
    rates = BattleRates()
    tr.update(_inp(now=0.0, gap_ahead_s=0.8, drs_available=True), TH, rates)
    # passed: P4, the old rival is now the car behind
    b = tr.update(
        _inp(now=12.0, position=4, ahead_idx=3, behind_idx=1, gap_ahead_s=6.0, gap_behind_s=2.0),
        TH,
        rates,
    )
    assert tr.drain() == [Episode("attack", 1, 10, 10, True, "passed")]
    assert b.result == "passed" and b.result_recent and b.result_rival_idx == 1
    late = tr.update(
        _inp(now=60.0, position=4, ahead_idx=3, behind_idx=1, gap_ahead_s=6.0), TH, rates
    )
    assert late.result == "" and not late.result_recent


def test_defend_episode_held_and_lost() -> None:
    tr = BattleTracker()
    tr.update(_inp(now=0.0, gap_behind_s=0.6), TH, BattleRates())
    tr.update(_inp(now=30.0, gap_behind_s=2.5), TH, BattleRates())
    assert [e.result for e in tr.drain()] == ["held"]
    tr.update(_inp(now=40.0, gap_behind_s=0.4), TH, BattleRates())
    tr.update(
        _inp(now=41.0, position=6, ahead_idx=2, behind_idx=4, gap_ahead_s=0.3), TH, BattleRates()
    )
    assert [e.result for e in tr.drain()] == ["lost"]


def test_short_failed_attack_not_learned() -> None:
    tr = BattleTracker()
    tr.update(_inp(now=0.0, gap_ahead_s=0.9), TH, BattleRates())
    tr.update(_inp(now=2.0, gap_ahead_s=3.0), TH, BattleRates())
    assert tr.drain() == []


def test_shrink() -> None:
    assert shrink(None, 0, 0.35, 4) == 0.35
    assert shrink(1.0, 4, 0.0, 4) == 0.5


def test_engine_folds_episodes_into_pass_model(tmp_path: Path) -> None:
    db = Database(":memory:")
    engine = build_engine(
        clock=VirtualClock(), sinks=[], db=db, decision_log_path=tmp_path / "d.jsonl"
    )
    snap = dataclasses.replace(Snapshot(now=0.0), track_id=7)
    prior = engine.battle_rates(7).pass_drs
    for _ in range(4):
        engine._persist_episode(snap, Episode("attack", 1, 3, 4, True, "passed"))
    p = db.get_param(7, PASS_COMPOUND, PASS_DRS)
    assert p is not None and p.value == 1.0
    assert engine.battle_rates(7).pass_drs > prior


def test_strategy_payload_battle() -> None:
    snap = dataclasses.replace(
        Snapshot(now=0.0),
        session_type=15,
        session_kind="race",
        race_phase="racing",
        lap_num=20,
        laps_remaining=30,
        battle_mode="catching",
        battle_catch_laps=4.0,
        battle_closing_ahead_s=0.5,
        battle_pass_prob=0.35,
    )
    s = strategy_payload(snap)
    assert s is not None
    assert s["battle"]["mode"] == "catching"
    assert s["battle"]["catch_laps"] == 4.0
    assert s["battle"]["threat_laps"] is None
    assert s["battle"]["result"] is None


@pytest.fixture(scope="module")
def battle_runs(tmp_path_factory: pytest.TempPathFactory):
    specs = {
        "attack": RaceSpec(laps=6, gap_ahead_s=0.8),
        "defend": RaceSpec(laps=6, gap_behind_s=0.7),
        "free": RaceSpec(laps=6, gap_ahead_s=12.0, gap_behind_s=12.0),
    }
    return {k: _replay(tmp_path_factory.mktemp(k), s) for k, s in specs.items()}


def test_replay_attack_call(battle_runs) -> None:
    calls, rows = battle_runs["attack"]
    assert "battle_attack" in _ids(calls)
    row = next(r for r in rows if r.get("rule_id") == "battle_attack" and r["outcome"] == "fired")
    assert row["inputs"]["battle_mode"] == "attacking"


def test_replay_defend_call(battle_runs) -> None:
    calls, _ = battle_runs["defend"]
    assert "battle_defend" in _ids(calls)
    assert "battle_attack" not in _ids(calls)


def test_replay_free_air_is_quiet(battle_runs) -> None:
    calls, _ = battle_runs["free"]
    assert not {i for i in _ids(calls) if i and i.startswith("battle_")}
