from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.metrics import Metrics
from pitwall.protocol.layouts import Corners
from pitwall.rules.engine import RuleEngine
from pitwall.server.app import state_payload
from pitwall.state.runplan import COOL, HOT, HotLap, Plan, RunTracker, mistakes_text, run_plan
from pitwall.state.session import Snapshot

PUSH = Plan("push", "ready")


def _plan(**kw: object) -> Plan:
    base: dict[str, object] = {
        "margin_ms": -500,
        "margin_kind": "cut",
        "safe_margin_ms": 1000,
        "ers_pct": 60.0,
        "ers_min_pct": 20.0,
        "hottest_c": 98.0,
        "tyre_hot_c": 104.0,
        "time_left_s": 600.0,
        "cool_lap_s": 98.0,
        "fuel_laps": 10.0,
    }
    base.update(kw)
    return run_plan(**base)  # type: ignore[arg-type]


def test_run_plan_decisions() -> None:
    assert _plan() == Plan("push", "ready")
    assert _plan(ers_pct=2.0) == Plan("cool", "battery")
    assert _plan(hottest_c=105.0) == Plan("cool", "tyres")
    assert _plan(margin_ms=1200) == Plan("box", "safe")
    assert _plan(margin_ms=1200, margin_kind="pole") == Plan("box", "safe")
    assert _plan(ers_pct=2.0, time_left_s=60.0) == Plan("push_now", "time")
    assert _plan(ers_pct=2.0, fuel_laps=2.5) == Plan("push_now", "fuel")
    assert _plan(fuel_laps=1.5) == Plan("box", "fuel")
    assert _plan(time_left_s=0.0) == Plan("box", "flag")
    # unknown margin (no field times yet) is never "safe"
    assert _plan(margin_ms=0, margin_kind="", ers_pct=5.0) == Plan("cool", "battery")


def test_mistakes_text() -> None:
    lap = HotLap(76_200, 20_700, 39_800, 15_700, 64.0, 6.0, 2, 1, True)
    assert mistakes_text(lap, (19_200, 39_700, 15_600)) == (
        "2 lock-ups, a spin, lap invalid, 1.5 lost in sector 1 to your best"
    )
    clean = HotLap(75_400, 19_200, 39_700, 15_650, 50.0, 2.0, 0, 0, False)
    assert mistakes_text(clean, (19_200, 39_700, 15_600)) == ""


def _feed(
    rt: RunTracker,
    t: float,
    ms: int,
    dist: float,
    sector: int,
    *,
    s1: int = 0,
    ers: float = 50.0,
    plan: Plan = PUSH,
    phase: str = "flying",
    best_s1: int = 20_000,
) -> HotLap | None:
    return rt.update(
        t=t,
        phase=phase,
        lap_time_ms=ms,
        lap_distance=dist,
        sector=sector,
        sector1_ms=s1,
        sector2_ms=0,
        invalid=False,
        ers_pct=ers,
        lockups=0,
        spins=0,
        best_s1_ms=best_s1,
        cool_pace_pct=10.0,
        decide=lambda: plan,
    )


def test_run_tracker_hot_cool_transitions() -> None:
    rt = RunTracker()
    _feed(rt, 0.0, 0, 100.0, 0, phase="out_lap")
    assert rt.kind == "out"
    _feed(rt, 1.0, 100, 10.0, 0)
    assert rt.kind == HOT and rt.crossings == 1
    _feed(rt, 60.0, 75_000, 5000.0, 2)
    done = _feed(rt, 61.0, 50, 5.0, 0, ers=3.0, plan=Plan("cool", "battery"))
    assert done is not None and done.lap_time_ms == 75_000 and done.ers_end_pct == 3.0
    assert rt.kind == COOL and rt.plan == Plan("cool", "battery") and rt.crossings == 2
    # driver ignores the plan and pushes through sector 1: back to a hot lap
    _feed(rt, 70.0, 20_000, 1500.0, 0)
    _feed(rt, 71.0, 20_500, 1600.0, 1, s1=20_400)
    assert rt.kind == HOT
    # next lap: plan says push but sector 1 is slow -> driver is cooling
    _feed(rt, 130.0, 75_000, 5000.0, 2)
    _feed(rt, 131.0, 50, 5.0, 0)
    _feed(rt, 160.0, 26_000, 1600.0, 1, s1=25_000)
    assert rt.kind == COOL
    # a cool lap ending produces no HotLap and the next lap starts hot
    _feed(rt, 230.0, 100_000, 5000.0, 2)
    assert _feed(rt, 231.0, 50, 5.0, 0) is None
    assert rt.kind == HOT and rt.plan == Plan("", "")
    _feed(rt, 300.0, 0, 50.0, 0, phase="in_lap")
    assert rt.kind == ""


def test_run_tracker_ignores_flashback() -> None:
    rt = RunTracker()
    _feed(rt, 0.0, 0, 100.0, 0, phase="out_lap")
    _feed(rt, 1.0, 100, 10.0, 0)
    _feed(rt, 40.0, 40_000, 3000.0, 1)
    rt.note_rewind()
    assert _feed(rt, 41.0, 30_000, 300.0, 1) is None
    assert rt.crossings == 1


def _rules() -> RuleEngine:
    settings = ConfigStore().current()
    return RuleEngine(list(settings.rules), thresholds=settings.thresholds, mode={})


def _qsnap(**kw: object) -> Snapshot:
    base: dict[str, object] = {
        "now": 0.0,
        "session_kind": "qualifying",
        "session_type": 5,
        "lap_num": 3,
        "phase": "flying",
        "_ages": {k: 0.1 for k in ("lap_data", "car_status", "car_telemetry", "session_history")},
    }
    base.update(kw)
    return Snapshot(**base)  # type: ignore[arg-type]


def _ids(snap: Snapshot) -> dict[str, str]:
    return {c.rule.defn.id: c.text for c in _rules().evaluate(snap).candidates}


def test_plan_rules_at_the_line() -> None:
    ids = _ids(
        _qsnap(
            sector=0,
            run_plan="cool",
            run_plan_reason="battery",
            run_plan_why="Battery's 2",
            ers_store_pct=2.0,
        )
    )
    assert "plan_cool" in ids and ids["plan_cool"].startswith("Battery's 2.")
    ids = _ids(
        _qsnap(
            sector=0,
            run_plan="push_now",
            run_plan_reason="time",
            run_plan_why="2 minutes left",
            ers_store_pct=4.0,
        )
    )
    assert "plan_push_now" in ids
    ids = _ids(_qsnap(sector=0, run_plan="box", run_plan_reason="safe", quali_margin_s=1.3))
    assert "plan_box_safe" in ids and "plan_box" not in ids
    assert not {k for k in _ids(_qsnap(sector=1, run_plan="cool")) if k.startswith("plan_")}


def test_cool_lap_coaching_rules() -> None:
    cool = {"cool_lap": True, "run_lap_kind": "cool", "run_plan": "cool"}
    # plan_cool already said "recharge"; the separate reminder is for driver-initiated cools
    assert "cool_recharge" not in _ids(_qsnap(sector=0, **cool))
    assert "cool_recharge" in _ids(_qsnap(sector=0, cool_lap=True, run_plan="push"))
    # recharge is deploy mode 0; the check nags only once the cool lap is under way
    assert "cool_recharge_check" in _ids(_qsnap(cool_elapsed_s=30.0, ers_deploy_mode=2, **cool))
    assert "cool_recharge_check" not in _ids(_qsnap(cool_elapsed_s=5.0, ers_deploy_mode=2, **cool))
    assert "cool_recharge_check" not in _ids(_qsnap(cool_elapsed_s=30.0, ers_deploy_mode=0, **cool))
    ids = _ids(
        _qsnap(
            sector=1,
            pole_gap_ms=2_200,
            pole_gap_s=2.2,
            pole_worst_sector=2,
            pole_worst_sector_s=1.9,
            last_hot_mistakes="a lock-up",
            cool_tyre_hint="tyres fine",
            **cool,
        )
    )
    assert {"cool_status", "cool_vs_pole", "cool_mistakes"} <= set(ids)
    assert ids["cool_vs_pole"] in (
        "Pole is 2.2 up. Most of it is sector 2, 1.9",
        "2.2 to pole, 1.9 of that in sector 2",
    )
    assert "cool_car_behind" in _ids(_qsnap(hot_car_behind_s=2.0, **cool))
    assert "cool_car_behind" not in _ids(_qsnap(hot_car_behind_s=math.inf, **cool))
    ids = _ids(_qsnap(cool_prep=True, tyre_inner_front_c=96.0, **cool))
    assert "cool_hot_mode" in ids and "cool_status" not in ids


def test_cool_payload_swaps_layout() -> None:
    settings = ConfigStore().current()

    def payload(**kw: object) -> dict[str, object]:
        p = state_payload(_qsnap(**kw), settings=settings, metrics=Metrics(), quiet=False)
        q = p["quali"]
        assert isinstance(q, dict)
        return q

    q = payload(
        cool_lap=True,
        run_plan="cool",
        run_plan_reason="battery",
        ers_store_pct=12.0,
        track_length_m=5000.0,
        dist_to_hot_mode_m=1800.0,
        tyre_inner_ema_fast=Corners(90.0, 105.0, 92.0, 93.0),
        pole_gap_ms=2_000,
        pole_sector_gaps_ms=(100, 1_700, 200),
        hot_car_behind_s=math.inf,
    )
    cool = q["cool"]
    assert isinstance(cool, dict)
    assert cool["ers_pct"] == 12.0 and cool["dist_to_hot_m"] == 1800.0
    assert cool["ers_min_pct"] == 40 and cool["window_c"] == [88, 102]
    assert cool["pole"] == {"driver": None, "gap_ms": 2_000, "sector_gaps_ms": [100, 1_700, 200]}
    assert cool["car_behind_s"] is None and cool["mistakes"] is None
    assert q["plan"] == {"plan": "cool", "reason": "battery"}
    # hot-lap-mode point reached: normal layout returns
    assert "cool" not in payload(cool_lap=True, cool_prep=True, run_plan="cool")
    assert "cool" not in payload(cool_lap=False)


Q1 = Path(os.environ.get("PITWALL_Q1_RECORDING", "~/recordings/q1.f1bin.zst")).expanduser()


@pytest.mark.skipif(not Q1.exists(), reason="set PITWALL_Q1_RECORDING to a real Q1 recording")
def test_q1_recording_run_plan(tmp_path: Path) -> None:
    import asyncio
    import json

    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine, run_replay

    log = tmp_path / "d.jsonl"
    engine = build_engine(clock=VirtualClock(), decision_log_path=log)
    asyncio.run(run_replay(Q1, engine, None))
    fired = [
        d["rule_id"]
        for d in map(json.loads, log.read_text().splitlines())
        if d.get("outcome") == "fired" and str(d.get("rule_id", "")).startswith("plan_")
    ]
    # Q1 run 1 (2026-09-25): battery 31% after lap 1, then 2% and 6% at the line
    assert fired[:3] == ["plan_push", "plan_cool", "plan_cool"]


def test_run_tracker_extends_cool_lap() -> None:
    rt = RunTracker()
    _feed(rt, 0.0, 0, 100.0, 0, phase="out_lap")
    _feed(rt, 1.0, 100, 10.0, 0)
    _feed(rt, 60.0, 75_000, 5000.0, 2)
    _feed(rt, 61.0, 50, 5.0, 0, ers=0.0, plan=Plan("cool", "battery"))
    assert rt.kind == COOL
    _feed(rt, 150.0, 90_000, 5000.0, 2)
    rt.update(
        t=151.0,
        phase="flying",
        lap_time_ms=50,
        lap_distance=5.0,
        sector=0,
        sector1_ms=0,
        sector2_ms=0,
        invalid=False,
        ers_pct=34.0,
        lockups=0,
        spins=0,
        best_s1_ms=0,
        cool_pace_pct=10.0,
        decide=lambda: Plan("push", "ready"),
        extend_cool=True,
    )
    assert rt.kind == COOL and rt.plan == Plan("cool", "battery")


def test_fuel_thresholds_configurable() -> None:
    # Q2 recording: 2.84 laps at the line was enough for cool + hot + in.
    assert _plan(ers_pct=0.0, fuel_laps=2.84, fuel_cool_laps=2.2) == Plan("cool", "battery")
    assert _plan(ers_pct=0.0, fuel_laps=2.0, fuel_cool_laps=2.2) == Plan("push_now", "fuel")
