from __future__ import annotations

import dataclasses

import pytest

import pitwall.engine.planning as planning
from pitwall.engine import _hold_cover_plan, build_engine
from pitwall.model.deg import DegFit
from pitwall.state.session import Damage as DamageView
from pitwall.state.session import Snapshot
from pitwall.strategy.pitwindow import NO_PLAN


def test_vsc_ending_reprices_pit_plan_at_green_loss(tmp_path, monkeypatch) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    fit = DegFit(90_000.0, 40.0, 30.0, 20, 0.0, 0.9, "prior")
    engine.deg_fit = fit
    engine.ctx.fresh_prior = fit
    engine.ctx.green_pit_loss_s = 22.0
    engine.ctx.sc_pit_loss_s = 12.0
    updates: list[bool] = []
    original_update = engine.planner.plan_tracker.update

    def record_update(*args, **kwargs):
        updates.append(kwargs["neutralised"])
        return original_update(*args, **kwargs)

    monkeypatch.setattr(engine.planner.plan_tracker, "update", record_update)
    snap = Snapshot(
        now=1.0,
        session_kind="race",
        phase="vsc",
        race_phase="vsc",
        safety_car_status=2,
        lap_num=10,
        laps_remaining=20,
        tyre_age_laps=10,
        tyre_visual=17,
        wear_mean_pct=50.0,
        laps_of_pace=20.0,
        pit_loss_s=12.0,
        pit_loss_source="session",
        _ages={"lap_data": 0.1, "car_status": 0.1},
    )

    vsc = engine.planner._plan(snap)
    assert vsc.pit_plan == "cheap_stop"
    assert "box_now" in {
        candidate.rule.id for candidate in engine.rule_engine.evaluate(vsc).candidates
    }

    ending = engine.planner._plan(dataclasses.replace(snap, sc_ending=True))

    assert ending.pit_plan != "cheap_stop"
    assert "box_now" not in {
        candidate.rule.id for candidate in engine.rule_engine.evaluate(ending).candidates
    }
    assert updates == [True, False]


def test_model_view_carries_green_pit_loss(tmp_path) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    engine.model._update_model()
    engine.ctx.green_pit_loss_s = 22.0
    engine.ctx.sc_pit_loss_s = 12.0
    engine.model._update_model()

    assert engine.state.model.green_pit_loss_s == 22.0
    assert engine.state.model.sc_pit_loss_s == 12.0


def test_pitted_true_rival_cover_keeps_confidence_above_mode_min(tmp_path) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    engine.deg_fit = DegFit(90_000.0, 100.0, 30.0, 20, 0.0, 0.9, "prior")
    engine.ctx.fresh_prior = DegFit(90_000.0, 300.0, 30.0, 20, 0.0, 0.9, "prior")
    snap = Snapshot(
        now=1.0,
        phase="racing",
        race_phase="racing",
        lap_num=10,
        laps_remaining=20,
        tyre_age_laps=10,
        wear_mean_pct=30.0,
        laps_of_pace=5.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
        gap_behind_s=1.5,
        rival_behind_idx=2,
        rival_behind_name="PIASTRI",
        rival_behind_pace_ms=90_000,
        rival_behind_pitted=True,
        true_rival_behind_idx=2,
        true_rival_behind_name="PIASTRI",
        true_rival_behind_gap_s=0.2,
        true_rival_behind_pitted=True,
    )

    planned = engine.planner._plan(snap)

    assert planned.pit_plan == "cover"
    assert planned.pit_plan_confidence >= 0.70
    assert next(iter(engine.planner._plan_mc_cache))[-4:] == ("cover", 2, False, 500)


def test_held_cover_plan_survives_until_next_racing_lap() -> None:
    cover = dataclasses.replace(NO_PLAN, plan="cover", lap=5, rival_idx=2, rival_name="PIASTRI")
    fresh = dataclasses.replace(NO_PLAN, plan="box_in_n")
    held = (cover, 5)

    for lap_num in (5, 6):
        selected, next_held = _hold_cover_plan(
            held,
            fresh,
            Snapshot(now=float(lap_num), phase="racing", lap_num=lap_num, laps_remaining=10),
            2,
        )
        assert selected == cover
        assert next_held == held

    after_window, cleared = _hold_cover_plan(
        held,
        fresh,
        Snapshot(now=7.0, phase="racing", lap_num=7, laps_remaining=10),
        2,
    )
    assert after_window == fresh
    assert cleared is None

    after_phase_change, cleared = _hold_cover_plan(
        held,
        fresh,
        Snapshot(now=6.0, phase="sc", lap_num=6, laps_remaining=10),
        2,
    )
    assert after_phase_change == fresh
    assert cleared is None


def test_five_lap_race_plans_without_a_mandatory_stop(tmp_path) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    thresholds = engine.controls._plan_thresholds

    assert thresholds(Snapshot(now=1.0))["plan_two_compound_rule"] == 1
    assert thresholds(Snapshot(now=1.0, short_race=True))["plan_two_compound_rule"] == 0
    assert thresholds(Snapshot(now=1.0))["plan_max_stops"] != 0
    assert thresholds(Snapshot(now=1.0, short_race=True))["plan_max_stops"] == 0
    assert thresholds(Snapshot(now=1.0, is_sprint=True))["plan_max_stops"] == 0


def _no_stop_snap(**kw) -> Snapshot:
    base = dict(
        now=1.0,
        session_kind="race",
        phase="racing",
        race_phase="racing",
        lap_num=2,
        laps_remaining=3,
        tyre_visual=17,
        tyre_age_laps=1,
        laps_of_pace=20.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
        _ages={"lap_data": 0.1, "car_status": 0.1},
    )
    return Snapshot(**{**base, **kw})


def _no_stop_engine(tmp_path, monkeypatch, deg=217.0):
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    fit = DegFit(90_000.0, deg, 30.0, 20, 0.0, 0.9, "prior")
    engine.ctx.deg_fit = fit
    engine.ctx.fresh_prior = fit
    engine.ctx.green_pit_loss_s = 22.0
    engine.ctx.sc_pit_loss_s = 12.0
    calls: list[int] = []
    original = planning.optimise

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(planning, "optimise", spy)
    return engine, calls


@pytest.mark.parametrize(
    "snap",
    [
        _no_stop_snap(short_race=True, laps_remaining=4),
        _no_stop_snap(is_sprint=True, laps_remaining=23),
    ],
    ids=["five-lap-race", "sprint"],
)
def test_no_stop_session_plans_no_stop_without_optimiser(tmp_path, monkeypatch, snap) -> None:
    engine, calls = _no_stop_engine(tmp_path, monkeypatch)

    planned = engine.planner._plan(snap)

    assert calls == []
    assert planned.plan_label.startswith("no-stop")
    assert planned.plan_target_lap == 0
    assert len(planned.plans) == 1
    assert planned.pit_plan == ""


def test_no_stop_session_under_safety_car_does_not_cheap_stop(tmp_path, monkeypatch) -> None:
    engine, calls = _no_stop_engine(tmp_path, monkeypatch)
    snap = _no_stop_snap(
        is_sprint=True,
        phase="sc",
        race_phase="sc",
        safety_car_status=1,
        lap_num=10,
        laps_remaining=14,
        tyre_age_laps=9,
        wear_mean_pct=40.0,
    )

    planned = engine.planner._plan(snap)

    assert calls == []
    assert planned.pit_plan != "cheap_stop"


def test_no_stop_session_with_wing_damage_still_calls_a_stop(tmp_path, monkeypatch) -> None:
    engine, calls = _no_stop_engine(tmp_path, monkeypatch, deg=60.0)
    snap = _no_stop_snap(
        is_sprint=True,
        laps_remaining=23,
        damage=DamageView(front_left_wing=60),
    )

    planned = engine.planner._plan(snap)

    assert calls == [1]
    assert planned.pit_plan in ("box_now", "box_in_n")
    active = engine.planner.plan_tracker.state.active_plan
    assert active is not None and active.stops >= 1
    assert planned.plan_next_compound
    assert snap.lap_num < planned.plan_target_lap <= snap.lap_num + snap.laps_remaining


def test_wet_crossover_still_calls_inters_in_a_sprint(tmp_path) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    dry = _no_stop_snap(
        is_sprint=True,
        weather_crossover="",
        tyre_compound=17,
        _ages={"session": 0.1},
    )
    engine.rule_engine.evaluate(dry)
    snap = dataclasses.replace(dry, weather_crossover="to_inter")

    assert "weather_to_inter" in {
        candidate.rule.id for candidate in engine.rule_engine.evaluate(snap).candidates
    }
