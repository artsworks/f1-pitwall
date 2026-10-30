from __future__ import annotations

import dataclasses

from pitwall.engine import _hold_cover_plan, build_engine
from pitwall.model.deg import DegFit
from pitwall.state.session import Snapshot
from pitwall.strategy.pitwindow import NO_PLAN


def test_pitted_true_rival_cover_keeps_confidence_above_mode_min(tmp_path) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    engine.deg_fit = DegFit(90_000.0, 100.0, 30.0, 20, 0.0, 0.9, "prior")
    engine._fresh_prior = DegFit(90_000.0, 300.0, 30.0, 20, 0.0, 0.9, "prior")
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

    planned = engine._plan(snap)

    assert planned.pit_plan == "cover"
    assert planned.pit_plan_confidence >= 0.70
    assert next(iter(engine._plan_mc_cache))[-4:] == ("cover", 2, False, 500)


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
