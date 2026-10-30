"""Named strategy plans: derivation (feasibility, ranking, windows, Plan C)
and the tracker's switch / off-plan / invalidation behaviour."""

import dataclasses

import pytest

import pitwall.strategy.plans as plan_module
from pitwall.config.loader import ConfigStore
from pitwall.model.deg import DegFit, fit_stint
from pitwall.store.db import LapRow
from pitwall.strategy.plans import (
    CompoundModel,
    PlanEval,
    PlanFields,
    PlanInputs,
    PlanTracker,
    alternative,
    derive,
    evaluate,
    feasible,
    p_faster,
    reactive,
    risk_quantile,
    spoken,
    view_fields,
)

TH = ConfigStore().current().thresholds
S, M, H = 16, 17, 18


def fit(deg: float) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 10, 100.0, 0.9, "fit")


def _lap(age: int, time_ms: float) -> LapRow:
    return LapRow(
        id=age,
        session_uid=1,
        car_idx=0,
        lap_num=age + 1,
        lap_time_ms=int(time_ms),
        s1_ms=0,
        s2_ms=0,
        compound=18,
        tyre_age_laps=age,
        fuel_remaining_laps=20.0,
        valid=1,
        invalid_reasons=(),
        wear_pct=0.0,
        fuel_kg=0.0,
        ers_deployed_j=0.0,
        sc_status=0,
        weather=0,
    )


def models(
    s_deg: float = 160.0, m_deg: float = 100.0, h_deg: float = 60.0, sets: int = 2
) -> tuple[CompoundModel, ...]:
    return (
        CompoundModel(S, 89_600.0, s_deg, sets),
        CompoundModel(M, 90_000.0, m_deg, sets),
        CompoundModel(H, 90_400.0, h_deg, sets),
    )


def inputs(**kw: object) -> PlanInputs:
    base = PlanInputs(
        lap_num=1,
        laps_remaining=50,
        tyre_age=0,
        current=M,
        cur=fit(100.0),
        cur_cliff_age=40.0,
        compounds=models(),
        used=frozenset({M}),
        green_loss_s=22.0,
        sc_loss_s=11.0,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def test_damage_invalidation_flips_slots_and_latches() -> None:
    tracker = PlanTracker()
    kw = dict(stops_done=0, neutralised=False, cheap_stop=False)
    tracker.update(inputs(), TH, **kw)
    state = tracker.update(inputs(), TH, invalidate="damage", **kw)
    assert (state.active, state.switched_from, state.switch_reason, state.switch_lap) == (
        "B",
        "A",
        "invalid",
        1,
    )
    count = state.switch_count
    assert tracker.update(inputs(), TH, invalidate="damage", **kw).switch_count == count
    tracker.reset()
    tracker.update(inputs(), TH, **kw)
    state = tracker.update(
        inputs(), TH, invalidate="damage", stops_done=0, neutralised=True, cheap_stop=False
    )
    assert state.active == "A"
    state = tracker.update(inputs(lap_num=2), TH, **kw)
    assert (state.active, state.switch_reason, state.switch_lap) == ("B", "invalid", 2)


def test_damage_penalty_only_applies_before_first_stop() -> None:
    clean = inputs()
    damaged = inputs(damage_pace_ms=1500.0)
    assert evaluate(damaged, (M,), TH).race_time_s - evaluate(
        clean, (M,), TH
    ).race_time_s == pytest.approx(75.0)
    assert evaluate(damaged, (M, H), TH, stop_now=True) == evaluate(
        clean, (M, H), TH, stop_now=True
    )


def test_two_compound_rule_forces_a_stop_onto_another_compound() -> None:
    inp = inputs()
    assert not feasible(inp, (M,), TH)
    assert not feasible(inp, (M, M), TH)
    assert feasible(inp, (M, H), TH)
    assert feasible(inp, (M, M, H), TH)
    # already raced two dry compounds: no-stop is legal again
    assert feasible(inputs(used=frozenset({S, M})), (M,), TH)
    # rule disabled
    assert feasible(inp, (M,), {**TH, "plan_two_compound_rule": 0})


def test_tyre_sets_limit_sequences() -> None:
    no_hards = (
        CompoundModel(S, 89_600.0, 160.0, 2),
        CompoundModel(M, 90_000.0, 100.0, 1),
        CompoundModel(H, 90_400.0, 60.0, 0),
    )
    inp = inputs(compounds=no_hards)
    assert not feasible(inp, (M, H), TH)
    assert not feasible(inp, (M, M, M), TH)  # one medium set left
    assert feasible(inp, (M, S, S), TH)
    assert all(H not in e.compounds for e in derive(inp, TH))
    # unknown set counts (no Tyre Sets packet yet) don't restrict
    assert feasible(inputs(compounds=models(sets=-1)), (M, H, H), TH)


def test_derive_ranked_with_windows() -> None:
    inp = inputs()
    ranked = derive(inp, TH)
    assert ranked
    times = [e.race_time_s for e in ranked]
    assert times == sorted(times)
    for e in ranked:
        assert e.compounds[0] == M
        assert len(e.compounds) - 1 == len(e.stop_laps)
        if e.stop_laps:
            lo, hi = e.window
            assert inp.lap_num <= lo <= e.stop_laps[0] <= hi <= inp.lap_num + inp.laps_remaining
    # a 50-lap race on these models is a one-stop medium -> hard
    assert ranked[0].compounds == (M, H)


def test_evaluate_race_time_spread_adds_shared_slope_age_sums() -> None:
    compound_models = tuple(
        dataclasses.replace(model, deg_posterior_sd_ms_per_lap=3.0) for model in models()
    )
    inp = inputs(
        tyre_age=10,
        cur=dataclasses.replace(fit(100.0), deg_posterior_sd_ms_per_lap=2.0),
        compounds=compound_models,
    )
    result = evaluate(inp, (M, H), TH)
    assert result is not None and len(result.stop_laps) == 1

    current_laps = result.stop_laps[0] - inp.lap_num
    fresh_laps = inp.laps_remaining - current_laps
    current_sd_ms = 2.0 * (current_laps * inp.tyre_age + current_laps * (current_laps - 1) / 2)
    fresh_sd_ms = 3.0 * (fresh_laps * (fresh_laps - 1) / 2)
    expected = (current_sd_ms + fresh_sd_ms) / 1000.0
    assert result.race_time_sd_s == expected
    assert evaluate(inputs(), (M, H), TH).race_time_sd_s == 0.0  # type: ignore[union-attr]


def test_evaluate_race_time_spread_zero_when_fits_are_exact() -> None:
    prior = DegFit(90_000.0, 80.0, 30.0, 0, 0.0, 0.3, "prior")
    laps = [_lap(age, 90_000 + 20 * age + 6 * age**2) for age in range(12)]
    curved = fit_stint(laps, prior, min_laps=3, fuel_coeff_fixed=30.0, quadratic=True)
    assert curved.source == "fit" and curved.deg2_ms_per_lap2 > 0
    assert curved.deg_posterior_sd_ms_per_lap < 1e-6

    inp = inputs(tyre_age=10, cur=curved)
    result = evaluate(inp, (M, H), TH)
    assert result is not None and len(result.stop_laps) == 1
    assert result.race_time_sd_s == pytest.approx(0.0, abs=1e-6)

    old = evaluate(
        inputs(
            tyre_age=10,
            cur=dataclasses.replace(curved, deg_posterior_sd_ms_per_lap=curved.deg_sd_ms_per_lap),
        ),
        (M, H),
        TH,
    )
    assert old is not None and old.race_time_sd_s > 1.0


def test_alternative_uses_downside_score_and_keeps_ranked_order_at_zero_spread() -> None:
    primary = PlanEval((M, H), (20,), (20, 20), 98.0)
    first = PlanEval((M, H, S), (20, 30), (20, 20), 99.0, 10.0)
    lower_downside = PlanEval((M, S, H, M), (20, 30, 40), (20, 20), 100.0, 0.0)
    ranked = [primary, first, lower_downside]

    assert alternative(ranked, primary, z=0.0) is first
    assert alternative(ranked, primary, z=0.84) is lower_downside


def _patch_tracker_evaluations(
    monkeypatch: pytest.MonkeyPatch, *, best_time: float, sd_a: float, sd_b: float
) -> None:
    seq_a = (M, H)
    seq_b = (M, S, H)
    a_initial = PlanEval(seq_a, (20,), (20, 20), 100.0, sd_a)
    b_initial = PlanEval(seq_b, (20, 30), (20, 20), 103.0, sd_b)
    b_best = PlanEval(seq_b, (20, 30), (20, 20), best_time, sd_b)
    a_current = PlanEval(seq_a, (20,), (20, 20), 100.0, sd_a)
    stage = 0

    def fake_derive(*_args: object) -> list[PlanEval]:
        nonlocal stage
        ranked = [a_initial, b_initial] if stage == 0 else [b_best, a_current]
        stage += 1
        return ranked

    def fake_evaluate(
        _inp: object, seq: tuple[int, ...], *_args: object, **_kw: object
    ) -> PlanEval:
        return a_current if seq == seq_a else b_initial if stage == 1 else b_best

    monkeypatch.setattr(plan_module, "derive", fake_derive)
    monkeypatch.setattr(plan_module, "evaluate", fake_evaluate)
    monkeypatch.setattr(plan_module, "feasible", lambda *_args: True)
    monkeypatch.setattr(plan_module, "reactive", lambda *_args: None)


def test_tracker_switches_on_probability_with_large_fixed_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_tracker_evaluations(monkeypatch, best_time=97.0, sd_a=1.0, sd_b=1.0)
    tracker = PlanTracker()
    tracker.update(
        inputs(),
        {**TH, "plan_off_s": 0.5, "plan_switch_s": 1000.0, "plan_switch_prob": 0.9},
        stops_done=0,
        neutralised=False,
        cheap_stop=False,
    )
    state = tracker.update(
        inputs(lap_num=2),
        {**TH, "plan_off_s": 0.5, "plan_switch_s": 1000.0, "plan_switch_prob": 0.9},
        stops_done=0,
        neutralised=False,
        cheap_stop=False,
    )
    assert state.active == "B" and state.switch_reason == "pace"


def test_tracker_does_not_switch_when_spread_makes_probability_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_tracker_evaluations(monkeypatch, best_time=90.0, sd_a=20.0, sd_b=1.0)
    tracker = PlanTracker()
    tracker.update(
        inputs(),
        {**TH, "plan_off_s": 0.5, "plan_switch_s": 5.0, "plan_switch_prob": 0.9},
        stops_done=0,
        neutralised=False,
        cheap_stop=False,
    )
    state = tracker.update(
        inputs(lap_num=2),
        {**TH, "plan_off_s": 0.5, "plan_switch_s": 5.0, "plan_switch_prob": 0.9},
        stops_done=0,
        neutralised=False,
        cheap_stop=False,
    )
    assert state.active == "A" and state.off_s == 10.0


def test_tracker_switches_when_shared_slope_exposure_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inp = inputs(cur=dataclasses.replace(fit(100.0), deg_posterior_sd_ms_per_lap=2.0))
    assert inp.cur.deg_posterior_sd_ms_per_lap > 0.0
    assert all(model.deg_posterior_sd_ms_per_lap == 0.0 for model in inp.compounds)

    seq_a = (M, H)
    seq_b = (M, S)
    stop_laps = (20,)
    current_sd = (
        inp.cur.deg_posterior_sd_ms_per_lap * (19 * inp.tyre_age + 19 * (19 - 1) / 2) / 1000.0
    )
    a_current = PlanEval(seq_a, stop_laps, (20, 20), 100.0, current_sd)
    b_initial = PlanEval(seq_b, stop_laps, (20, 20), 104.0, current_sd)
    b_best = PlanEval(seq_b, stop_laps, (20, 20), 96.0, current_sd)
    stage = 0

    def fake_derive(*_args: object) -> list[PlanEval]:
        nonlocal stage
        stage += 1
        return [a_current, b_initial] if stage == 1 else [b_best, a_current]

    def fake_evaluate(
        _inp: object, seq: tuple[int, ...], *_args: object, **_kw: object
    ) -> PlanEval:
        return a_current if seq == seq_a else b_initial if stage == 1 else b_best

    monkeypatch.setattr(plan_module, "derive", fake_derive)
    monkeypatch.setattr(plan_module, "evaluate", fake_evaluate)
    monkeypatch.setattr(plan_module, "feasible", lambda *_args: True)
    monkeypatch.setattr(plan_module, "reactive", lambda *_args: None)

    th = {**TH, "plan_off_s": 3.0, "plan_switch_s": 5.0, "plan_switch_prob": 0.9}
    tracker = PlanTracker()
    tracker.update(inp, th, stops_done=0, neutralised=False, cheap_stop=False)
    state = tracker.update(
        dataclasses.replace(inp, lap_num=2),
        th,
        stops_done=0,
        neutralised=False,
        cheap_stop=False,
    )

    assert len(a_current.compounds) - 1 == len(b_best.compounds) - 1 == 1
    assert a_current.stop_laps == b_best.stop_laps == stop_laps
    assert p_faster(4.0, current_sd, current_sd) == 1.0
    assert state.active == "B" and state.switch_reason == "pace"


def test_plan_tracker_clamps_risk_quantile_endpoints() -> None:
    assert risk_quantile({**TH, "plan_mc_risk_quantile": 0.0}) == 0.001
    assert risk_quantile({**TH, "plan_mc_risk_quantile": 1.0}) == 0.999

    for quantile in (0.0, 1.0):
        PlanTracker().update(
            inputs(),
            {**TH, "plan_mc_risk_quantile": quantile},
            stops_done=0,
            neutralised=False,
            cheap_stop=False,
        )


def test_high_deg_prefers_two_stop() -> None:
    hot = inputs(cur=fit(400.0), compounds=models(s_deg=640.0, m_deg=400.0, h_deg=240.0))
    assert len(derive(hot, TH)[0].compounds) == 3


def test_reactive_plan_c_boxes_now_at_neutralised_loss() -> None:
    inp = inputs(lap_num=20, laps_remaining=31, tyre_age=19)
    c = reactive(inp, TH)
    assert c is not None
    assert c.stop_laps[0] == inp.lap_num
    green = reactive(dataclasses.replace(inp, sc_loss_s=inp.green_loss_s), TH)
    assert green is not None
    assert c.race_time_s < green.race_time_s


def test_tracker_sets_a_and_b_and_announces() -> None:
    tr = PlanTracker()
    st = tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    ids = {p.id: p for p in st.plans}
    assert st.active == "A" and st.on_plan
    assert set(ids) == {"A", "B", "C"}
    assert ids["A"].delta_s == 0.0
    assert ids["B"].stops != ids["A"].stops
    assert ids["B"].delta_s > 0
    ev = tr.drain()
    assert [(e.kind, e.to_plan, e.reason) for e in ev] == [("set", "A", "start")]
    f = view_fields(st, 1)
    assert f.plan_spoken == "one stop, medium then hard"
    assert f.plan_next_compound == "hard"
    assert f.plan_target_lap == ids["A"].stop_laps[0]
    assert f.plan_b_spoken.startswith("two stop")


def test_tracker_sc_switch_to_c_then_back_when_not_taken() -> None:
    tr = PlanTracker()
    tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    tr.drain()
    inp = inputs(lap_num=15, laps_remaining=36, tyre_age=14)
    st = tr.update(inp, TH, stops_done=0, neutralised=True, cheap_stop=True)
    assert st.active == "C" and st.switch_reason == "sc" and st.target_lap == 15
    assert [(e.from_plan, e.to_plan) for e in tr.drain()] == [("A", "C")]
    # SC in and the stop wasn't taken: Plan C is gone
    inp = inputs(lap_num=17, laps_remaining=34, tyre_age=16)
    st = tr.update(inp, TH, stops_done=0, neutralised=False, cheap_stop=False)
    assert st.active in ("A", "B") and st.switch_reason == "invalid"
    assert [(e.from_plan, e.reason) for e in tr.drain()] == [("C", "invalid")]


def test_tracker_sc_stop_taken_keeps_c() -> None:
    tr = PlanTracker()
    tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    c = tr.update(
        inputs(lap_num=15, laps_remaining=36, tyre_age=14),
        TH,
        stops_done=0,
        neutralised=True,
        cheap_stop=True,
    ).active_plan
    assert c is not None and c.id == "C" and c.stop_laps[0] == 15
    new = c.compounds[1]
    tr.drain()
    st = tr.update(
        inputs(lap_num=17, laps_remaining=34, tyre_age=1, current=new, used=frozenset({M, new})),
        TH,
        stops_done=1,
        neutralised=False,
        cheap_stop=False,
    )
    assert st.active == "C" and st.stops_left == c.stops - 1
    assert st.sequence == "-".join("SMH"[x - S] for x in c.compounds[1:])
    assert tr.drain() == []


def test_tracker_goes_off_plan_then_switches_on_pace() -> None:
    tr = PlanTracker()
    tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    tr.drain()
    # hards turn out far worse than modelled: M-H loses to M-S-S by a margin
    bad = inputs(lap_num=5, laps_remaining=46, tyre_age=4, compounds=models(h_deg=400.0))
    st = tr.update(bad, TH, stops_done=0, neutralised=False, cheap_stop=False)
    assert st.switch_reason == "pace" and st.switched_from == "A"
    assert H not in st.active_plan.compounds  # type: ignore[union-attr]
    assert st.on_plan
    assert [e.kind for e in tr.drain()] == ["switch"]


def test_tracker_off_plan_below_switch_threshold() -> None:
    tr = PlanTracker()
    th = {**TH, "plan_off_s": 3.0, "plan_switch_s": 1000.0}
    tr.update(inputs(), th, stops_done=0, neutralised=False, cheap_stop=False)
    tr.drain()
    bad = inputs(lap_num=5, laps_remaining=46, tyre_age=4, compounds=models(h_deg=400.0))
    st = tr.update(bad, th, stops_done=0, neutralised=False, cheap_stop=False)
    assert st.active == "A" and not st.on_plan and st.off_s > 3.0
    assert [e.kind for e in tr.drain()] == ["off"]
    st = tr.update(
        inputs(lap_num=6, laps_remaining=45, tyre_age=5),
        th,
        stops_done=0,
        neutralised=False,
        cheap_stop=False,
    )
    assert st.on_plan
    assert [e.kind for e in tr.drain()] == ["on"]


def test_spoken_forms() -> None:
    tr = PlanTracker()
    st = tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    a = st.active_plan
    assert a is not None
    assert a.label.startswith("1-stop")
    assert spoken(None) == ""


def test_plan_fields_mirrored_on_snapshot_and_model_view() -> None:
    from pitwall.state.model_view import ModelView
    from pitwall.state.session import Snapshot

    plan_names = {x.name for x in dataclasses.fields(PlanFields)}
    assert plan_names <= {x.name for x in dataclasses.fields(Snapshot)}
    assert plan_names <= {x.name for x in dataclasses.fields(ModelView)}


def test_soft_bias_flips_ranking_to_softer_next_stint() -> None:
    # 10 laps left with stop_now=True (or tyre_age=39 past cliff=30):
    # fresh stint is 10 laps (ages 0..9, age_sum=45).
    # H: 10 * 90_000 + 60 * 45 = 902_700 ms (902.7 s)
    # S: 10 * 90_000 + 80 * 45 = 903_600 ms (903.6 s) -> 0.9 s slower than H
    close_models = (
        CompoundModel(S, 90_000.0, 80.0, 2),
        CompoundModel(M, 90_000.0, 100.0, 2),
        CompoundModel(H, 90_000.0, 60.0, 2),
    )
    inp = inputs(laps_remaining=10, tyre_age=35, cur_cliff_age=10.0, compounds=close_models)
    mh = evaluate(inp, (M, H), TH)
    ms = evaluate(inp, (M, S), TH)
    assert mh is not None and ms is not None
    gap_s = ms.race_time_s - mh.race_time_s
    assert 0.0 < gap_s < 1.5

    bal = derive(inp, {**TH, "plan_soft_bias_s": 0.0})
    aggr = derive(inp, {**TH, "plan_soft_bias_s": 1.5})
    assert bal[0].compounds == (M, H)
    assert aggr[0].compounds == (M, S)


def _close_inputs(**kw: object) -> PlanInputs:
    # Same numbers as the soft-bias test: medium then hard is 0.9 s faster than
    # medium then soft, so a 1.5 s soft bias flips the ranking.
    close_models = (
        CompoundModel(S, 90_000.0, 80.0, 2),
        CompoundModel(M, 90_000.0, 100.0, 2),
        CompoundModel(H, 90_000.0, 60.0, 2),
    )
    base = inputs(
        laps_remaining=10,
        tyre_age=35,
        cur_cliff_age=10.0,
        compounds=close_models,
        used=frozenset({S, M}),
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


BAL = {**TH, "plan_soft_bias_s": 0.0, "plan_pick": "confidence"}
AGGR = {**TH, "plan_soft_bias_s": 1.5, "plan_pick": "gain"}


def test_tracker_reseed_rebuilds_a_from_new_ranking_and_keeps_stops() -> None:
    tr = PlanTracker()
    st = tr.update(
        _close_inputs(lap_num=40), BAL, stops_done=1, neutralised=False, cheap_stop=False
    )
    assert st.active == "A" and view_fields(st, 40).plan_next_compound == "hard"
    tr.drain()

    # a lap change under the new mindset is not a mindset change: A stays
    st = tr.update(
        _close_inputs(lap_num=41), AGGR, stops_done=1, neutralised=False, cheap_stop=False
    )
    assert tr.seqs["A"] == (1, (M, H))
    assert view_fields(st, 41).plan_next_compound == "hard"
    assert not [e for e in tr.drain() if e.kind in ("set", "switch")]

    st = tr.update(
        _close_inputs(lap_num=41),
        AGGR,
        stops_done=1,
        neutralised=False,
        cheap_stop=False,
        reseed=True,
    )
    assert st.active == "A" and view_fields(st, 41).plan_next_compound == "soft"
    assert tr.seqs["A"] == (1, (M, S))
    assert tr.seqs["B"][0] == 1 and tr.seqs["B"][1] != (M, S)
    assert st.stops_left == 1
    assert [(e.kind, e.to_plan, e.reason) for e in tr.drain()] == [("set", "A", "mindset")]


def test_tracker_reseed_during_sc_goes_back_to_plan_c() -> None:
    tr = PlanTracker()
    inp = _close_inputs(lap_num=40)
    tr.update(inp, BAL, stops_done=1, neutralised=True, cheap_stop=True)
    assert tr.active == "C"
    tr.drain()
    st = tr.update(inp, AGGR, stops_done=1, neutralised=True, cheap_stop=True, reseed=True)
    assert st.active == "C"
    assert tr.seqs["A"] == (1, (M, S))
    assert [(e.kind, e.to_plan, e.reason) for e in tr.drain()] == [
        ("set", "A", "mindset"),
        ("switch", "C", "sc"),
    ]


def test_engine_reseeds_plans_only_on_mindset_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine

    from .factories import make_snapshot

    engine = build_engine(clock=VirtualClock(), sinks=[])
    calls: list[bool] = []

    def fake_update(*_args: object, reseed: bool = False, **_kw: object) -> object:
        calls.append(reseed)
        return engine.plan_tracker.state

    monkeypatch.setattr(engine.plan_tracker, "update", fake_update)
    pit = SimpleNamespace(plan="stay")
    snap = make_snapshot(race_phase="racing", tyre_visual=M, laps_remaining=20, lap_num=5)
    engine._plans(snap, fit(100.0), pit)  # type: ignore[arg-type]
    engine._plans(dataclasses.replace(snap, lap_num=6), fit(100.0), pit)  # type: ignore[arg-type]
    engine.mindset_override = "aggressive"
    engine._plans(dataclasses.replace(snap, lap_num=6), fit(100.0), pit)  # type: ignore[arg-type]
    engine._plans(dataclasses.replace(snap, lap_num=7), fit(100.0), pit)  # type: ignore[arg-type]
    assert calls == [False, False, True, False]
