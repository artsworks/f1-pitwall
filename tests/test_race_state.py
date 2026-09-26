"""RacePhase machine: formation -> racing -> sc/vsc -> in_lap -> out_lap -> finished."""

from __future__ import annotations

from pitwall.state.race import RacePhase


def _step(rp: RacePhase, t: float, **kw: object) -> str:
    args: dict[str, object] = dict(
        session_time=t,
        safety_car_status=0,
        pit_status=0,
        driver_status=4,
        result_status=2,
        lap_num=1,
        lap_boundary=False,
        lights_out_seen=True,
        chequered_seen=False,
        red_flag=False,
        sc_exit_hold_s=3.0,
    )
    args.update(kw)
    return rp.update(**args)  # type: ignore[arg-type]


def test_formation_until_lights_out() -> None:
    rp = RacePhase()
    assert _step(rp, 0.0, safety_car_status=3, lights_out_seen=False) == "formation"
    assert _step(rp, 1.0, lap_num=0, lights_out_seen=False) == "formation"
    assert _step(rp, 2.0, lap_num=1, lights_out_seen=True) == "racing"


def test_sc_exit_hysteresis() -> None:
    rp = RacePhase()
    _step(rp, 0.0)
    assert _step(rp, 1.0, safety_car_status=1) == "sc"
    assert _step(rp, 2.0, safety_car_status=0) == "sc"  # within hold
    assert _step(rp, 3.5, safety_car_status=0) == "sc"
    assert _step(rp, 4.5, safety_car_status=0) == "racing"
    assert _step(rp, 5.0, safety_car_status=2) == "vsc"


def test_sc_laps_count() -> None:
    rp = RacePhase()
    _step(rp, 0.0)
    _step(rp, 1.0, safety_car_status=1, lap_boundary=True)
    _step(rp, 2.0, safety_car_status=1, lap_boundary=True)
    assert rp.sc_laps == 2
    _step(rp, 10.0, lap_boundary=True)
    assert rp.sc_laps == 0


def test_pit_sequence() -> None:
    rp = RacePhase()
    assert _step(rp, 0.0) == "racing"
    assert _step(rp, 1.0, pit_status=1) == "in_lap"
    assert _step(rp, 2.0, pit_status=2) == "in_lap"
    assert _step(rp, 3.0, pit_status=0) == "out_lap"
    assert _step(rp, 4.0, pit_status=0) == "out_lap"
    assert _step(rp, 5.0, pit_status=0, lap_boundary=True, lap_num=2) == "racing"


def test_driver_status_drives_in_out_lap() -> None:
    rp = RacePhase()
    assert _step(rp, 0.0, driver_status=3) == "out_lap"


def test_finished_and_red_flag() -> None:
    rp = RacePhase()
    _step(rp, 0.0)
    assert _step(rp, 1.0, chequered_seen=True) == "racing"
    assert _step(rp, 2.0, chequered_seen=True, lap_boundary=True) == "finished"
    assert _step(rp, 3.0, safety_car_status=1) == "finished"
    rp2 = RacePhase()
    assert _step(rp2, 0.0, red_flag=True) == "red_flag"
