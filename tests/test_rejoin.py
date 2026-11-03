import math

import numpy as np
import pytest

from pitwall.config.loader import ConfigStore
from pitwall.model.deg import DegFit
from pitwall.strategy.montecarlo import plan_agreement
from pitwall.strategy.pitwindow import optimise
from pitwall.strategy.rejoin import FieldCar, rejoin_position

SETTINGS = ConfigStore().current()
TH = SETTINGS.thresholds
BALANCED = SETTINGS.resolved_mindset()
AGGRESSIVE = {
    **BALANCED,
    **{k: v for k, v in SETTINGS.mindsets["aggressive"].items() if k != "inherits"},
}


def fit(deg: float, conf: float = 0.9) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 10, 100.0, conf, "fit")


def run(**kw: object):
    args: dict[str, object] = dict(
        lap_num=10,
        laps_remaining=20,
        tyre_age=10,
        wear_mean=30.0,
        fit=fit(150),
        fresh=fit(80),
        laps_of_pace=50.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
        green_pit_loss_s=21.0,
        sc_status=0,
        rival_ahead=None,
        rival_behind=None,
        gap_ahead_s=math.inf,
        gap_behind_s=math.inf,
        pit_exit_clean=False,
        restricted=False,
        mode=BALANCED,
        th=TH,
    )
    args.update(kw)
    return optimise(**args)  # type: ignore[arg-type]


# A car 21 s behind us running 1.6+ s a lap faster lands us 0.8 s behind it
# only when we stop now (k=0); by k=1 its lead is clear air.
FAST_CHASER = FieldCar("NORRIS", 21.0, 88_400, 0.0, False)
DEG200 = dict(fit=fit(200), tyre_age=12)


def test_clean_air_rejoin_position() -> None:
    field = (
        FieldCar("LEADER", -30.0, 90_000, 100.0, False),
        FieldCar("NORRIS", 30.0, 90_000, 100.0, False),
    )
    rejoin = rejoin_position(
        field,
        k=2,
        our_lap_ms=lambda j: 90_000.0,
        pit_loss_s=21.0,
        warmup_s=0.8,
        dirty_air_gap_s=1.5,
    )
    assert rejoin.position == 2
    assert rejoin.ahead_name == "LEADER"
    assert rejoin.gap_to_ahead_s == pytest.approx(51.7)
    assert not rejoin.in_traffic

    clear = rejoin_position(
        field[1:],
        k=2,
        our_lap_ms=lambda j: 90_000.0,
        pit_loss_s=21.0,
        warmup_s=0.8,
        dirty_air_gap_s=1.5,
    )
    assert clear.position == 1
    assert clear.ahead_name == ""
    assert clear.gap_to_ahead_s == math.inf
    assert not clear.in_traffic


def test_traffic_shifts_best_lap_by_one() -> None:
    base = run(**DEG200)
    traffic = run(**DEG200, field=(FAST_CHASER,))
    assert base.plan == "box_now" and base.lap == 10
    assert traffic.lap - base.lap == 1
    assert traffic.projections[0][1] == round(base.projections[0][1] + 1.2, 2)
    assert traffic.rejoin_pos == 2
    assert traffic.rejoin_ahead_name == "NORRIS"


def test_agreement_uses_the_same_traffic_penalties() -> None:
    plan = run(**DEG200, field=(FAST_CHASER,))
    assert plan.lap == 11
    th = {
        "plan_mc_sims": 200,
        "plan_mc_sc_prob_per_lap": 0.0,
        "plan_mc_deg_sd_frac": 0.0,
        "plan_mc_pit_loss_sd_s": 0.0,
        "pit_horizon_laps": 8,
        "pit_min_laps_left": 2,
        "outlap_warmup_s": 0.8,
        "tyre_cliff_ms": 1500,
        "cliff_ms_per_lap": 1000,
    }
    kw = dict(
        lap_num=10,
        laps_remaining=20,
        tyre_age=12,
        fit=fit(200),
        fresh=fit(80),
        cur_cliff_age=62.0,
        pit_loss_s=21.0,
        pit_loss_samples_s=(),
        sc_pit_loss_s=9.0,
        plan_lap=plan.lap,
        mode={"pit_gain_min_s": 0.4},
        th=th,
    )
    with_penalties = plan_agreement(
        **kw, traffic_penalties_s=plan.traffic_penalties_s, rng=np.random.default_rng(9)
    )
    without = plan_agreement(**kw, rng=np.random.default_rng(9))
    assert with_penalties is not None and without is not None
    assert with_penalties.modal_lap == plan.lap
    assert with_penalties.agree > without.agree


def test_empty_field_matches_current_behaviour() -> None:
    assert run() == run(field=())
    plan = run(**DEG200)
    assert plan.rejoin_pos is None
    assert plan.rejoin_ahead_name is None
    assert plan.rejoin_gap_s is None
    assert plan.traffic_penalties_s == ()


def test_reason_names_rejoin_position_without_duplicate_traffic_risk() -> None:
    # deg 300 keeps the stop now best even after the 1.2 s traffic penalty,
    # so the chosen lap itself lands in dirty air.
    p = run(
        fit=fit(300),
        tyre_age=12,
        gap_behind_s=11.0,
        field=(FAST_CHASER,),
        mode=AGGRESSIVE,
    )
    assert p.plan == "box_now" and p.lap == 10
    assert p.risky
    assert "rejoin P2 in traffic behind NORRIS" in p.reason
    assert p.reason.count("traffic") == 1
