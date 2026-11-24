"""Plan C repriced after an SC stop: the stint on the fitted set must stay
inside the set's remaining usable life (usable_life_laps - tyre_age)."""

import dataclasses

from pitwall.config.loader import ConfigStore
from pitwall.model.deg import DegFit
from pitwall.strategy.plans import (
    CompoundModel,
    PlanInputs,
    PlanTracker,
    evaluate,
)

TH = ConfigStore().current().thresholds
S, M, H = 16, 17, 18


def fit(deg: float) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 10, 100.0, 0.9, "fit")


def models() -> tuple[CompoundModel, ...]:
    return (
        CompoundModel(S, 89_600.0, 250.0, 2, usable_life_laps=12),
        CompoundModel(M, 90_000.0, 300.0, 2, usable_life_laps=18),
        CompoundModel(H, 90_400.0, 281.0, 2, usable_life_laps=23),
    )


def inputs(**kw: object) -> PlanInputs:
    base = PlanInputs(
        lap_num=1,
        laps_remaining=28,
        tyre_age=0,
        current=H,
        cur=fit(281.0),
        cur_cliff_age=40.0,
        compounds=models(),
        used=frozenset({H}),
        green_loss_s=22.0,
        sc_loss_s=11.0,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def test_evaluate_first_stint_capped_by_fitted_usable_life() -> None:
    # Softs fitted one lap ago with a 12-lap life leave 11 laps; the medium
    # stint is cheaper enough that the uncapped optimum stops at lap 22.
    inp = inputs(
        lap_num=8,
        laps_remaining=21,
        tyre_age=1,
        current=S,
        cur=fit(250.0),
        used=frozenset({H, S}),
    )
    e = evaluate(inp, (S, M), TH)
    assert e is not None
    assert e.stop_laps[0] <= 19
    assert e.window[1] <= 19


def test_evaluate_spent_fitted_set_forces_stop_now() -> None:
    # 12-lap softs already 12 laps old: only k1 == 0 (box this lap) fits, and
    # the medium set covers the remaining 18 laps exactly.
    inp = inputs(
        lap_num=8,
        laps_remaining=18,
        tyre_age=12,
        current=S,
        cur=fit(250.0),
        used=frozenset({H, S}),
    )
    e = evaluate(inp, (S, M), TH)
    assert e is not None
    assert e.stop_laps == (8,)


def test_plan_c_second_stop_respects_fitted_usable_life() -> None:
    """The report's case: a two-stop Plan C (hards, softs, mediums) is taken
    under SC on lap 7; after the softs are fitted the same strategy must not
    move its second stop past lap 19."""
    tr = PlanTracker()
    tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    tr.drain()
    st = tr.update(
        inputs(lap_num=7, laps_remaining=22, tyre_age=12),
        TH,
        stops_done=0,
        neutralised=True,
        cheap_stop=True,
        sc_sequence=(H, S, M),
    )
    assert st.active == "C"
    c = next(p for p in st.plans if p.id == "C")
    assert c.compounds == (H, S, M) and c.stop_laps[0] == 7
    tr.drain()

    st = tr.update(
        inputs(
            lap_num=8,
            laps_remaining=21,
            tyre_age=1,
            current=S,
            cur=fit(250.0),
            used=frozenset({H, S}),
        ),
        TH,
        stops_done=1,
        neutralised=False,
        cheap_stop=False,
    )
    c = next(p for p in st.plans if p.id == "C")
    assert c.compounds == (S, M)
    assert c.stop_laps[0] <= 19
    assert c.window[1] <= 19
