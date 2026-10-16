"""Wet-weather crossover replays: net-gain pit calls (docs/03 "Weather")."""

from __future__ import annotations

import asyncio

import pytest

import pitwall.engine as engine_mod
from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.model.pitloss import current_pit_loss
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream, replay_race
from .synth import write_packet_stream

pytestmark = [pytest.mark.slow, pytest.mark.replay]


def _inters_quicker_extra(inter_ms: int, slick_ms: int) -> dict[str, object]:
    """Cars 1 and 3 run inters several seconds quicker; car 2 stays on slicks.

    The player is a slick car too, so both compound groups have >= 2 cars.
    """
    return {
        "car_status": {
            "cars": {
                1: {"actual_tyre_compound": 7, "visual_tyre_compound": 7, "tyres_age_laps": 2},
                2: {"actual_tyre_compound": 16, "visual_tyre_compound": 16, "tyres_age_laps": 2},
                3: {"actual_tyre_compound": 7, "visual_tyre_compound": 7, "tyres_age_laps": 2},
            }
        },
        "lap_data": {
            "cars": {
                1: {"last_lap_time_ms": inter_ms},
                2: {"last_lap_time_ms": slick_ms},
                3: {"last_lap_time_ms": inter_ms},
            }
        },
    }


def _fired(rows: list[dict]) -> set[str]:
    return {r.get("rule_id") for r in rows if r.get("outcome") == "fired"}


def test_net_gain_calls_inters(tmp_path) -> None:
    spec = RaceSpec(
        laps=15,
        base_ms=90_000,
        compound=17,
        rain_lap=2,
        rain_percentages=(100, 100),
        extra=_inters_quicker_extra(86_000, 90_000),
    )
    calls, rows = replay_race(tmp_path, spec)
    call = next(c for c in calls if c.rule_id == "tyre_switch")
    assert "inters" in call.text


def test_forecast_alone_never_calls_a_stop(tmp_path) -> None:
    spec = RaceSpec(
        laps=8,
        base_ms=90_000,
        compound=17,
        rain_lap=2,
        rain_percentages=(100, 100),
        extra={
            "car_status": {
                "cars": {
                    i: {
                        "actual_tyre_compound": 16,
                        "visual_tyre_compound": 16,
                        "tyres_age_laps": 2,
                    }
                    for i in (1, 2, 3)
                }
            },
            "lap_data": {"cars": {i: {"last_lap_time_ms": 90_000} for i in (1, 2, 3)}},
        },
    )
    _, rows = replay_race(tmp_path, spec)
    fired = _fired(rows)
    assert "weather_to_wet" in fired
    assert "tyre_switch" not in fired
    assert "weather_box_now" not in fired


def test_slip_trend_escalates_to_weather_box_now(tmp_path) -> None:
    spec = RaceSpec(
        laps=15,
        base_ms=90_000,
        compound=17,
        rain_lap=2,
        rain_percentages=(100, 100),
        traction_laps=(3,),
        traction_bursts=3,
        extra={"motion_ex": {}, **_inters_quicker_extra(86_000, 90_000)},
    )
    _, rows = replay_race(tmp_path, spec)
    fired = _fired(rows)
    assert "weather_box_now" in fired


def test_sc_pit_loss_prior_applies_mid_lap(tmp_path, monkeypatch) -> None:
    """The SC pit-loss prior must apply on the first SC tick, not the next lap."""
    engine = build_engine(clock=VirtualClock(), sinks=[], db=Database(":memory:"))
    refreshes: list[tuple[int, int]] = []
    real = current_pit_loss

    def spy(db, uid, track_id, sc_status, overlay, thresholds):
        prior = real(db, uid, track_id, sc_status, overlay, thresholds)
        if sc_status == engine.state.safety_car_status:
            refreshes.append((engine.state.lap_num, sc_status))
        return prior

    monkeypatch.setattr(engine_mod, "current_pit_loss", spy)
    path = write_packet_stream(
        tmp_path / "race.f1bin", race_stream(RaceSpec(laps=10, sc_laps=(5, 6)))
    )
    asyncio.run(run_replay(path, engine, None))
    # A prior refresh with sc_status 1 while still on lap 5 means the model
    # key noticed the SC deploying mid-lap, not at the lap 6 boundary.
    assert (5, 1) in refreshes
