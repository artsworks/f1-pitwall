import math

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.model.deg import DegFit
from pitwall.strategy.pitwindow import RivalView, optimise, race_time_s

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
        laps_of_pace=5.0,
        pit_loss_s=21.0,
        pit_loss_source="session",
        green_pit_loss_s=21.0,
        sc_status=0,
        rival_ahead=None,
        gap_ahead_s=math.inf,
        gap_behind_s=math.inf,
        pit_exit_clean=False,
        restricted=False,
        mode=BALANCED,
        th=TH,
    )
    args.update(kw)
    return optimise(**args)  # type: ignore[arg-type]


def test_race_time_stop_costs_pit_loss_plus_warmup() -> None:
    f = fit(0)
    none = race_time_s(
        stop_after=None,
        laps_remaining=5,
        tyre_age=0,
        cur=f,
        fresh=f,
        cur_cliff_age=math.inf,
        pit_loss_s=20.0,
        th=TH,
    )
    stop = race_time_s(
        stop_after=2,
        laps_remaining=5,
        tyre_age=0,
        cur=f,
        fresh=f,
        cur_cliff_age=math.inf,
        pit_loss_s=20.0,
        th=TH,
    )
    assert stop - none == pytest.approx(20.0 + float(TH["outlap_warmup_s"]))


def test_no_stop_when_tyres_last() -> None:
    p = run(laps_remaining=5, laps_of_pace=12.0, fit=fit(40))
    assert p.plan == "no_stop"
    assert p.lap == 0


def test_too_late_to_stop() -> None:
    p = run(laps_remaining=2)
    assert p.plan == "no_stop"
    assert p.reason == "too late to stop"
    assert p.undercut_s == p.overcut_s == 0.0


@pytest.mark.parametrize("laps_remaining", [1, 2])
def test_rival_pits_too_late_to_overcut(laps_remaining: int) -> None:
    p = run(
        laps_remaining=laps_remaining,
        rival_ahead=RivalView(1, "NORRIS", 92_500, True),
        gap_ahead_s=1.0,
        laps_of_pace=8.0,
        fit=fit(10),
        fresh=fit(10),
        tyre_age=2,
        mode=AGGRESSIVE,
    )
    assert p.plan == "no_stop"
    assert p.reason == "too late to stop"
    assert not p.projections
    assert p.undercut_s == p.overcut_s == 0.0


def test_overcut_requires_a_viable_stop() -> None:
    p = run(
        laps_remaining=3,
        th={**TH, "pit_min_laps_left": 3, "overcut_laps": 2},
        rival_ahead=RivalView(1, "NORRIS", 90_000, True),
        gap_ahead_s=20.0,
        laps_of_pace=8.0,
        fit=fit(10),
        fresh=fit(10),
        tyre_age=2,
        mode=AGGRESSIVE,
    )
    assert p.plan == "no_stop"
    assert not p.projections
    assert p.overcut_s == 0.0


def test_box_now_past_the_cliff() -> None:
    p = run(tyre_age=20, laps_of_pace=0.0, fit=fit(300))
    assert p.plan == "box_now"
    assert p.lap == 10
    assert p.window[0] == 10
    assert p.projections[0][0] == 10
    assert p.projections[0][1] < 0


def test_box_in_n_before_the_cliff() -> None:
    p = run(tyre_age=10, laps_of_pace=2.0, fit=fit(150), laps_remaining=12)
    assert p.plan in {"box_in_n", "box_now"}
    assert p.window[0] <= p.lap <= p.window[1]


def test_sc_cheap_stop_needs_wear() -> None:
    worn = run(sc_status=1, wear_mean=50.0, pit_loss_s=11.0)
    assert worn.plan == "cheap_stop"
    assert worn.gain_s == pytest.approx(10.0)
    fresh = run(sc_status=1, wear_mean=10.0, pit_loss_s=11.0, laps_of_pace=30.0)
    assert fresh.plan != "cheap_stop"


def test_aggressive_sc_stop_lower_wear() -> None:
    p = run(sc_status=2, wear_mean=30.0, pit_loss_s=11.0, mode=AGGRESSIVE)
    assert p.plan == "cheap_stop"
    assert run(sc_status=2, wear_mean=30.0, pit_loss_s=11.0).plan != "cheap_stop"


def test_undercut_on_slow_rival_ahead() -> None:
    rival = RivalView(1, "NORRIS", 92_500, False)
    p = run(rival_ahead=rival, gap_ahead_s=1.0, laps_of_pace=6.0)
    assert p.plan == "undercut"
    assert p.rival_name == "NORRIS"
    assert p.undercut_s >= float(BALANCED["undercut_speak_threshold_s"])


@pytest.mark.parametrize("laps_left_offset", [-1, 0, 1])
def test_undercut_requires_time_for_fresh_laps_and_stop(laps_left_offset: int) -> None:
    min_laps = int(TH["undercut_laps"]) + int(TH["pit_min_laps_left"])
    p = run(
        laps_remaining=min_laps + laps_left_offset,
        rival_ahead=RivalView(1, "NORRIS", 92_500, False),
        gap_ahead_s=1.0,
        tyre_age=20,
        laps_of_pace=0.0,
        fit=fit(300),
        mode={**BALANCED, "undercut_speak_threshold_s": 0.0},
    )
    assert p.projections
    assert p.window[0] == p.projections[0][0]
    if laps_left_offset <= 0:
        assert p.undercut_s == 0.0
        assert p.plan != "undercut"
    else:
        assert p.undercut_s > 0.0
        assert p.plan == "undercut"


def test_undercut_uses_session_seeded_base_pace() -> None:
    rival = RivalView(1, "NORRIS", 112_333, False)
    blended = DegFit(99_337.0, 60.0, 30.0, 10, 100.0, 0.9, "fit")
    seeded = DegFit(112_349.0, 60.0, 30.0, 10, 100.0, 0.9, "fit")

    old_base = run(rival_ahead=rival, gap_ahead_s=1.62, fit=blended, fresh=blended)
    session_base = run(rival_ahead=rival, gap_ahead_s=1.62, fit=seeded, fresh=seeded)

    assert old_base.undercut_s == pytest.approx(23.7, abs=0.1)
    assert session_base.undercut_s < 1.0


def test_no_undercut_when_gap_too_big() -> None:
    rival = RivalView(1, "NORRIS", 92_500, False)
    p = run(rival_ahead=rival, gap_ahead_s=8.0, laps_of_pace=6.0)
    assert p.plan != "undercut"


def test_overcut_after_rival_pits() -> None:
    rival = RivalView(1, "NORRIS", 90_000, True)
    p = run(
        rival_ahead=rival,
        gap_ahead_s=20.0,
        laps_of_pace=8.0,
        fit=fit(10),
        fresh=fit(10),
        tyre_age=2,
        mode=AGGRESSIVE,
    )
    assert p.overcut_s > 0
    assert p.plan == "overcut"
    balanced = run(
        rival_ahead=rival,
        gap_ahead_s=20.0,
        laps_of_pace=8.0,
        fit=fit(10),
        fresh=fit(10),
        tyre_age=2,
    )
    assert balanced.plan != "overcut"


@pytest.mark.parametrize("laps_remaining", [4, 5])
def test_overcut_requires_more_than_its_delay(laps_remaining: int) -> None:
    p = run(
        laps_remaining=laps_remaining,
        th={**TH, "overcut_laps": 4},
        rival_ahead=RivalView(1, "NORRIS", 90_000, True),
        gap_ahead_s=20.0,
        laps_of_pace=3.0,
        fit=fit(10),
        fresh=fit(10),
        tyre_age=2,
        mode={**AGGRESSIVE, "pit_gain_min_s": 0.0},
    )
    assert p.projections
    if laps_remaining == 4:
        assert p.overcut_s == 0.0
        assert p.plan != "overcut"
    else:
        assert p.overcut_s > 0.0
        assert p.plan == "overcut"


def test_free_stop_with_big_gap_behind() -> None:
    p = run(gap_behind_s=30.0, pit_exit_clean=True, tyre_age=15, laps_of_pace=0.0, fit=fit(300))
    assert p.plan == "free_stop"
    assert p.risk == 0.0


def test_position_loss_risk_scales_with_gap_behind() -> None:
    close = run(gap_behind_s=2.0, laps_of_pace=19.0)
    far = run(gap_behind_s=15.0, laps_of_pace=19.0)
    assert close.risk > far.risk > 0


def test_restricted_reduces_rival_plan_confidence_only() -> None:
    rival = RivalView(1, "NORRIS", 92_500, False)
    base = run(rival_ahead=rival, gap_ahead_s=1.0, laps_of_pace=6.0)
    restricted = run(rival_ahead=rival, gap_ahead_s=1.0, laps_of_pace=6.0, restricted=True)
    assert base.plan == restricted.plan == "undercut"
    assert restricted.confidence == pytest.approx(
        base.confidence * float(TH["restricted_confidence_factor"]), abs=1e-3
    )
    own = run(tyre_age=20, laps_of_pace=0.0, fit=fit(300))
    assert run(tyre_age=20, laps_of_pace=0.0, fit=fit(300), restricted=True) == own


def test_overlay_pit_loss_reduces_confidence() -> None:
    assert run(pit_loss_source="overlay").confidence < run().confidence


def test_forced_stop_has_no_position_risk() -> None:
    p = run(gap_behind_s=2.0, laps_of_pace=1.0, laps_remaining=20)
    assert p.risk == 0.0


def test_deterministic() -> None:
    assert run() == run()
