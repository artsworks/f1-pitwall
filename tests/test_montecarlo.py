from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

import pitwall.strategy.montecarlo as montecarlo
from pitwall.config.loader import ConfigStore
from pitwall.config.models import resolve_mindset
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
    plan_name: str = "",
    cover_gap_s: float = float("inf"),
    stack_s: float = 0.0,
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
        plan_name=plan_name,
        cover_gap_s=cover_gap_s,
        stack_s=stack_s,
    )


def test_same_seed_produces_the_same_result() -> None:
    fit = replace(
        _fit(150.0, sd=20.0),
        n=5,
        source="blend",
        deg_posterior_sd_ms_per_lap=80.0,
    )
    args = dict(fit=fit, fresh=_fit(80.0), plan_lap=10)

    assert _agreement(**args) == _agreement(**args)


def test_zero_stack_cost_matches_default() -> None:
    args = dict(fit=_fit(150.0, sd=20.0), fresh=_fit(80.0), plan_lap=10)

    assert _agreement(**args) == _agreement(**args, stack_s=0.0)


def test_stack_cost_reduces_box_now_agreement() -> None:
    args = dict(
        fit=_fit(130.0),
        fresh=_fit(80.0),
        plan_lap=10,
        laps_remaining=8,
        tyre_age=20,
        pit_loss_s=21.0,
        mode={"pit_gain_min_s": 1.0},
        th={
            **TH,
            "plan_mc_deg_sd_frac": 0.0,
            "plan_mc_pit_loss_sd_s": 0.0,
        },
    )

    base = _agreement(**args)
    stacked = _agreement(**args, stack_s=4.0)

    assert base is not None and stacked is not None
    assert base.agree > stacked.agree


def test_shipped_mindsets_keep_per_lap_mc_key() -> None:
    settings = ConfigStore(isolated=True).current()

    for mindset in ("balanced", "aggressive"):
        assert resolve_mindset(settings.mindsets, mindset)["mc_every_n_laps"] == 1


@pytest.mark.parametrize(("mindset", "expected"), [("balanced", 100), ("aggressive", 1000)])
@pytest.mark.parametrize("threshold_sims", [100, 0])
def test_shipped_mindsets_use_threshold_sims_unless_overridden(
    mindset: str, expected: int, threshold_sims: int
) -> None:
    settings = ConfigStore(isolated=True).current()
    mode = resolve_mindset(settings.mindsets, mindset)
    result = _agreement(
        fit=_fit(150.0),
        fresh=_fit(80.0),
        plan_lap=10,
        mode=mode,
        th={**TH, "plan_mc_sims": threshold_sims},
    )

    if threshold_sims == 0:
        assert result is None
    else:
        assert result is not None and result.sims == expected


def test_posterior_sampling_changes_thin_fit_but_zero_sd_keeps_legacy_draw() -> None:
    thin = replace(_fit(300.0), n=5, source="blend", deg_posterior_sd_ms_per_lap=1000.0)
    without_posterior = replace(thin, deg_posterior_sd_ms_per_lap=0.0)
    sampled = _agreement(fit=thin, fresh=_fit(0.0), plan_lap=11, laps_remaining=12, tyre_age=10)
    baseline = _agreement(
        fit=without_posterior, fresh=_fit(0.0), plan_lap=11, laps_remaining=12, tyre_age=10
    )
    assert sampled is not None and baseline is not None
    assert (sampled.modal_lap, sampled.agree) != (baseline.modal_lap, baseline.agree)

    full_fit = replace(_fit(150.0, sd=20.0), n=8, deg_posterior_sd_ms_per_lap=80.0)
    assert _agreement(fit=full_fit, fresh=_fit(80.0), plan_lap=10) == _agreement(
        fit=replace(full_fit, deg_posterior_sd_ms_per_lap=0.0),
        fresh=_fit(80.0),
        plan_lap=10,
    )


def test_risk_quantile_picks_low_spread_negative_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def candidate_deltas(**kwargs: object) -> np.ndarray:
        sims = len(kwargs["deg_samples"])  # type: ignore[arg-type]
        last_k = int(kwargs["last_k"])
        deltas = np.full((sims, last_k + 1), 50.0)
        deltas[:, 0] = np.array([-10.0, -9.0, -11.0, -10.0])
        deltas[:, 1] = np.array([-10.0, -10.1, -9.9, -10.0])
        return deltas

    monkeypatch.setattr(montecarlo, "_candidate_deltas_s", candidate_deltas)
    args = dict(
        fit=_fit(100.0),
        fresh=_fit(50.0),
        plan_lap=10,
        th={**TH, "plan_mc_sims": 4},
    )
    result = _agreement(**args)
    assert result is not None and result.q_modal_lap == 11

    monkeypatch.setattr(
        montecarlo,
        "_candidate_deltas_s",
        lambda **kwargs: np.ones((len(kwargs["deg_samples"]), int(kwargs["last_k"]) + 1)),
    )
    no_stop = _agreement(**args)
    assert no_stop is not None and no_stop.q_modal_lap == 0


def test_mc_risk_quantile_clamps_upper_endpoint() -> None:
    result = _agreement(
        fit=_fit(150.0),
        fresh=_fit(80.0),
        plan_lap=10,
        th={**TH, "plan_mc_risk_quantile": 1.0},
    )
    assert result is not None


def test_clear_stop_plan_has_high_agreement() -> None:
    result = _agreement(
        fit=_fit(500.0),
        fresh=_fit(0.0),
        plan_lap=10,
        tyre_age=12,
        cur_cliff_age=12.0,
    )

    assert result is not None and result.agree >= 0.95


def test_clear_cover_uses_sampled_cover_decision() -> None:
    fit = _fit(100.0)
    fresh = _fit(300.0)
    cover_gap_s = 0.2
    their_gain = ((100.0 * 10) + (100.0 * 11 - 300.0)) / 1000.0
    cover_s = their_gain - TH["outlap_warmup_s"] - cover_gap_s
    assert cover_s == pytest.approx(0.8)

    covered = _agreement(
        fit=fit,
        fresh=fresh,
        plan_lap=10,
        laps_remaining=12,
        tyre_age=10,
        cur_cliff_age=11.0,
        plan_name="cover",
        cover_gap_s=cover_gap_s,
    )
    old_scoring = _agreement(
        fit=fit,
        fresh=fresh,
        plan_lap=10,
        laps_remaining=12,
        tyre_age=10,
        cur_cliff_age=11.0,
        cover_gap_s=cover_gap_s,
    )
    stacked = _agreement(
        fit=fit,
        fresh=fresh,
        plan_lap=10,
        laps_remaining=12,
        tyre_age=10,
        cur_cliff_age=11.0,
        plan_name="cover",
        cover_gap_s=cover_gap_s,
        stack_s=4.0,
    )

    assert covered is not None and covered.modal_lap == 14 and covered.agree >= 0.9
    assert old_scoring is not None and old_scoring.agree < 0.1
    assert stacked is not None and stacked.agree < covered.agree

    plan = PitPlan("cover", 10, cover_s, 0.9, 0.0, 2, "PIASTRI", "", (10, 14), 0, 0, ())
    assert scale_plan(plan, covered).confidence >= 0.70


def test_marginal_cover_agreement_uses_sampled_degradation() -> None:
    fit = _fit(100.0, sd=30.0)
    fresh = _fit(300.0)
    their_gain = ((100.0 * 10) + (100.0 * 11 - 300.0)) / 1000.0
    cover_s = their_gain - TH["outlap_warmup_s"] - 1.5

    result = _agreement(
        fit=fit,
        fresh=fresh,
        plan_lap=10,
        laps_remaining=12,
        tyre_age=10,
        cur_cliff_age=11.0,
        plan_name="cover",
        cover_gap_s=1.5,
    )

    assert cover_s + 0.5 == pytest.approx(0.0)
    assert result is not None and 0.0 < result.agree < 1.0


def test_clear_defend_uses_sampled_cover_decision() -> None:
    result = _agreement(
        fit=_fit(20.0),
        fresh=_fit(30.0),
        plan_lap=12,
        tyre_age=2,
        plan_name="defend",
        cover_gap_s=2.0,
    )

    assert result is not None and result.agree >= 0.9


def test_box_plan_ignores_cover_agreement_arguments() -> None:
    args = {"fit": _fit(150.0, sd=20.0), "fresh": _fit(80.0), "plan_lap": 10}

    without_cover = _agreement(**args)
    with_cover = _agreement(**args, plan_name="box_now", cover_gap_s=0.2)

    assert with_cover == without_cover


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
    mc = McResult(2, 0.5, 500, 3, 10)

    scaled = scale_plan(plan, mc)
    cheap_stop = scale_plan(replace(plan, plan="cheap_stop"), mc)

    assert scaled.confidence == 0.4
    assert scaled.lap == plan.lap
    assert scaled.mc_lap == 12 and scaled.mc_agree == 0.5
    assert scaled.mc_risk_lap == 13
    assert cheap_stop == replace(plan, plan="cheap_stop")


@pytest.mark.parametrize(("offset", "expected"), [(None, 0), (0, 11), (2, 13)])
def test_scale_plan_translates_offsets_and_keeps_no_stop_distinct(
    offset: int | None, expected: int
) -> None:
    plan = PitPlan("box_now", 10, 3.0, 0.8, 0.2, -1, "", "", (10, 12), 0.0, 0.0, ())
    mc = McResult(offset, 0.5, 500, offset, 10)

    scaled = scale_plan(plan, mc, lap_num=11)

    assert scaled.mc_lap == expected
    assert scaled.mc_risk_lap == expected


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


def test_mc_mindset_sims_override_and_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine
    from pitwall.state.session import Snapshot

    res = _agreement(
        fit=_fit(150.0),
        fresh=_fit(80.0),
        plan_lap=10,
        mode={"pit_gain_min_s": 0.4, "plan_mc_sims": 750},
        th={**TH, "plan_mc_sims": 500},
    )
    assert res is not None and res.sims == 750

    capped = _agreement(
        fit=_fit(150.0),
        fresh=_fit(80.0),
        plan_lap=10,
        mode={"pit_gain_min_s": 0.4, "plan_mc_sims": 9999},
        th={**TH, "plan_mc_sims": 500},
    )
    assert capped is not None and capped.sims == 2000

    engine = build_engine(clock=VirtualClock(), sinks=[])
    engine.deg_fit = _fit(150.0)
    engine._fresh_prior = _fit(80.0)
    engine._green_pit_loss_s = 21.0
    engine._sc_pit_loss_s = 9.0
    engine.state.session_uid = 42
    calls = 0
    orig = montecarlo.plan_agreement

    def counted(**kw: object) -> McResult | None:
        nonlocal calls
        calls += 1
        return orig(**kw)  # type: ignore[arg-type]

    monkeypatch.setattr("pitwall.engine.plan_agreement", counted)
    monkeypatch.setattr(engine, "mode", lambda: {**MODE, "mc_every_n_laps": 2, "plan_mc_sims": 500})

    snap1 = Snapshot(
        now=1.0,
        session_kind="race",
        race_phase="racing",
        lap_num=1,
        laps_remaining=10,
        tyre_age_laps=1,
        laps_of_pace=15.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
    )
    snap2 = replace(snap1, lap_num=2, tyre_age_laps=2)
    snap3 = replace(snap1, lap_num=3, tyre_age_laps=3)

    def candidate_deltas(**kwargs: object) -> np.ndarray:
        deltas = np.ones((len(kwargs["deg_samples"]), int(kwargs["last_k"]) + 1))  # type: ignore[arg-type]
        deltas[:, 0] = -10.0
        return deltas

    monkeypatch.setattr(montecarlo, "_candidate_deltas_s", candidate_deltas)
    engine._plan(snap1)
    first = engine.pit_plan
    engine._plan(snap2)
    assert calls == 1
    assert first is not None and engine.pit_plan is not None
    assert (first.mc_lap, first.mc_risk_lap) == (1, 1)
    assert (engine.pit_plan.mc_lap, engine.pit_plan.mc_risk_lap) == (2, 2)
    engine._plan(snap3)
    assert calls == 2


def test_mc_same_lap_mindset_gain_threshold_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine
    from pitwall.state.session import Snapshot

    engine = build_engine(clock=VirtualClock(), sinks=[])
    engine.deg_fit = _fit(150.0)
    engine._fresh_prior = _fit(80.0)
    thresholds: list[object] = []
    mode = {**MODE, "plan_mc_sims": 500}
    original = montecarlo.plan_agreement

    def counted(**kwargs: object) -> McResult | None:
        thresholds.append(kwargs["mode"]["pit_gain_min_s"])  # type: ignore[index]
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("pitwall.engine.plan_agreement", counted)
    monkeypatch.setattr(engine, "mode", lambda: mode)
    snap = Snapshot(
        now=1.0,
        session_kind="race",
        race_phase="racing",
        lap_num=9,
        laps_remaining=10,
        tyre_age_laps=1,
        laps_of_pace=15.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
    )
    engine._plan(snap)
    mode["pit_gain_min_s"] = 0.4
    engine._plan(snap)
    assert thresholds == [1.0, 0.4]


def test_mc_stack_status_invalidates_cache_and_passes_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine
    from pitwall.state.session import Snapshot

    engine = build_engine(clock=VirtualClock(), sinks=[])
    engine.deg_fit = _fit(0.0)
    engine._fresh_prior = _fit(0.0)
    costs: list[object] = []

    def counted(**kwargs: object) -> McResult:
        costs.append(kwargs["stack_s"])
        return McResult(None, 1.0, 1)

    monkeypatch.setattr("pitwall.engine.plan_agreement", counted)
    monkeypatch.setattr(engine, "mode", lambda: {**MODE, "plan_mc_sims": 100})
    snap = Snapshot(
        now=1.0,
        session_kind="race",
        race_phase="racing",
        lap_num=10,
        laps_remaining=10,
        tyre_age_laps=1,
        laps_of_pace=15.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
    )

    engine._plan(snap)
    engine._plan(replace(snap, teammate_stacking=True))

    assert costs == [0.0, 4.0]
