"""Named strategy plans: derivation (feasibility, ranking, windows, Plan C)
and the tracker's switch / off-plan / invalidation behaviour."""

import dataclasses

from pitwall.config.loader import ConfigStore
from pitwall.model.deg import DegFit
from pitwall.strategy.plans import (
    CompoundModel,
    PlanFields,
    PlanInputs,
    PlanTracker,
    derive,
    feasible,
    reactive,
    spoken,
    view_fields,
)

TH = ConfigStore().current().thresholds
S, M, H = 16, 17, 18


def fit(deg: float) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 10, 100.0, 0.9, "fit")


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
