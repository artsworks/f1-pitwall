from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

import pitwall.strategy.montecarlo as montecarlo
from pitwall.model.deg import DegFit
from pitwall.strategy.montecarlo import McResult, plan_agreement, scale_plan
from pitwall.strategy.pitwindow import PitPlan, _fresh_fit, race_time_s

TH = {
    "plan_mc_sims": 500,
    "plan_mc_sc_prob_per_lap": 0.0,
    "plan_mc_deg_sd_frac": 0.15,
    "plan_mc_pit_loss_sd_s": 1.5,
    "pit_horizon_laps": 4,
    "pit_min_laps_left": 2,
    "outlap_warmup_s": 0.8,
    "tyre_cliff_ms": 1500,
    "cliff_ms_per_lap": 1000,
}
MODE = {"pit_gain_min_s": 1.0}


def _fit(deg: float, *, deg2: float = 0.0, sd: float = 0.0) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 20, 0.0, 0.9, "fit", False, deg2, sd)


def _agreement(
    *,
    fit: DegFit,
    fresh: DegFit,
    plan_lap: int,
    seed: int = 9,
    laps_remaining: int = 8,
    tyre_age: int = 5,
    pit_loss_s: float = 21.0,
    sc_pit_loss_s: float = 9.0,
    cur_cliff_age: float = 100.0,
    mode: dict[str, float] | None = None,
    th: dict[str, float] | None = None,
) -> McResult | None:
    return plan_agreement(
        lap_num=10,
        laps_remaining=laps_remaining,
        tyre_age=tyre_age,
        fit=fit,
        fresh=fresh,
        cur_cliff_age=cur_cliff_age,
        pit_loss_s=pit_loss_s,
        pit_loss_samples_s=(),
        sc_pit_loss_s=sc_pit_loss_s,
        plan_lap=plan_lap,
        mode=MODE if mode is None else mode,
        th=TH if th is None else th,
        rng=np.random.default_rng(seed),
    )


def test_same_seed_produces_the_same_result() -> None:
    args = dict(fit=_fit(150.0, sd=20.0), fresh=_fit(80.0), plan_lap=10)

    assert _agreement(**args) == _agreement(**args)


def test_clear_stop_plan_has_high_agreement() -> None:
    result = _agreement(
        fit=_fit(500.0),
        fresh=_fit(0.0),
        plan_lap=10,
        tyre_age=12,
        cur_cliff_age=12.0,
    )

    assert result is not None and result.agree >= 0.95


def test_near_equal_stop_options_reduce_agreement() -> None:
    result = _agreement(
        fit=_fit(1.0),
        fresh=_fit(1.05),
        plan_lap=10,
        tyre_age=0,
        pit_loss_s=2.0,
        mode={"pit_gain_min_s": 0.0},
        th={**TH, "plan_mc_deg_sd_frac": 0.15},
    )

    assert result is not None and result.agree < 1.0


def test_sc_every_lap_makes_current_lap_the_modal_stop() -> None:
    result = _agreement(
        fit=_fit(500.0),
        fresh=_fit(0.0),
        plan_lap=10,
        tyre_age=12,
        cur_cliff_age=12.0,
        sc_pit_loss_s=1.0,
        th={**TH, "plan_mc_sc_prob_per_lap": 1.0},
    )

    assert result is not None and result.modal_lap == 10


def test_zero_sims_and_short_horizon_skip_mc() -> None:
    fit = _fit(100.0)
    plan = PitPlan("box_now", 10, 3.0, 0.8, 0.2, -1, "", "", (10, 12), 0.0, 0.0, ())

    assert _agreement(fit=fit, fresh=_fit(50.0), plan_lap=10, th={**TH, "plan_mc_sims": 0}) is None
    assert (
        _agreement(
            fit=fit,
            fresh=_fit(50.0),
            plan_lap=10,
            laps_remaining=2,
            th={**TH, "pit_min_laps_left": 2},
        )
        is None
    )
    assert scale_plan(plan, None) == plan


def test_scale_plan_multiplies_confidence_but_leaves_cheap_stop_alone() -> None:
    plan = PitPlan("box_now", 10, 3.0, 0.8, 0.2, -1, "", "", (10, 12), 0.0, 0.0, ())
    mc = McResult(12, 0.5, 500)

    scaled = scale_plan(plan, mc)
    cheap_stop = scale_plan(replace(plan, plan="cheap_stop"), mc)

    assert scaled.confidence == 0.4
    assert scaled.lap == plan.lap
    assert scaled.mc_lap == 12 and scaled.mc_agree == 0.5
    assert cheap_stop == replace(plan, plan="cheap_stop")


def test_single_sim_delta_matches_deterministic_race_time(monkeypatch: pytest.MonkeyPatch) -> None:
    fit = _fit(72.0, deg2=3.0)
    fresh = _fit(18.0, deg2=99.0)
    laps_remaining = 7
    tyre_age = 6
    cur_cliff_age = 10.5
    pit_loss_s = 18.25
    captured: list[np.ndarray] = []
    original = montecarlo._candidate_deltas_s

    def capture(**kwargs: object) -> np.ndarray:
        deltas = original(**kwargs)  # type: ignore[arg-type]
        captured.append(deltas)
        return deltas

    monkeypatch.setattr(montecarlo, "_candidate_deltas_s", capture)
    result = plan_agreement(
        lap_num=20,
        laps_remaining=laps_remaining,
        tyre_age=tyre_age,
        fit=fit,
        fresh=fresh,
        cur_cliff_age=cur_cliff_age,
        pit_loss_s=pit_loss_s,
        pit_loss_samples_s=(),
        sc_pit_loss_s=5.0,
        plan_lap=20,
        mode={"pit_gain_min_s": 0.0},
        th={
            **TH,
            "plan_mc_sims": 1,
            "plan_mc_deg_sd_frac": 0.0,
            "plan_mc_pit_loss_sd_s": 0.0,
            "plan_mc_sc_prob_per_lap": 0.0,
        },
        rng=np.random.default_rng(12),
    )
    fresh_fit = _fresh_fit(fit, fresh)
    stop = race_time_s(
        stop_after=2,
        laps_remaining=laps_remaining,
        tyre_age=tyre_age,
        cur=fit,
        fresh=fresh_fit,
        cur_cliff_age=cur_cliff_age,
        pit_loss_s=pit_loss_s,
        th=TH,
    )
    no_stop = race_time_s(
        stop_after=None,
        laps_remaining=laps_remaining,
        tyre_age=tyre_age,
        cur=fit,
        fresh=fresh_fit,
        cur_cliff_age=cur_cliff_age,
        pit_loss_s=pit_loss_s,
        th=TH,
    )

    assert result is not None
    assert captured[0][0, 2] == pytest.approx(stop - no_stop, abs=1e-6)
