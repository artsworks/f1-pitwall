from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pitwall.state.model_view import ModelView
from pitwall.state.session import CarLap, Participant, SessionState
from tests.test_pitwindow import run

TRACK_M = 7004.0
SPEED_MPS = TRACK_M / 95.0
LEADER_DISTANCE = 100_000.0


def _car(
    position: int,
    race_time_s: float,
    *,
    front_gap_s: float = 0.0,
    pit_status: int = 0,
    num_pit_stops: int = 0,
    penalties: int = 0,
) -> CarLap:
    distance = LEADER_DISTANCE - race_time_s * SPEED_MPS
    return CarLap(
        lap_distance=distance % TRACK_M,
        total_distance=distance,
        car_position=position,
        delta_to_car_in_front_ms=round(front_gap_s * 1000),
        delta_to_race_leader_ms=round(race_time_s * 1000),
        num_pit_stops=num_pit_stops,
        penalties=penalties,
        pit_status=pit_status,
        result_status=2,
    )


def _history(*compounds: int) -> SimpleNamespace:
    return SimpleNamespace(
        num_tyre_stints=len(compounds),
        tyre_stints=tuple(
            SimpleNamespace(tyre_visual_compound=compound, end_lap=15) for compound in compounds
        ),
        num_laps=30,
        laps=(),
    )


def _session() -> SessionState:
    state = SessionState(thresholds={"plan_two_compound_rule": 1})
    state._player_idx = 2
    state.session_type = 15
    state.track_length_m = TRACK_M
    state.position = 3
    state.lap_distance = 3000.0
    state.delta_to_car_in_front_ms = 2700
    state.race_phase = "racing"
    state.participants = tuple(
        Participant(name=name) for name in ("VERSTAPPEN", "OCON", "HADJAR", "PIASTRI", "NORRIS")
    )
    state._last_session_time = 811.0
    state._last_update["lap_data"] = 811.0
    state._model = ModelView(pit_loss_s=22.0)
    state._histories = {
        0: _history(16, 17),
        1: _history(16, 17),
        2: _history(16),
        3: _history(16),
        4: _history(16, 17),
    }
    state.cars_lap = (
        _car(1, 0.0),
        _car(2, 10.0, front_gap_s=10.0),
        _car(3, 12.7, front_gap_s=2.7),
        _car(4, 14.2, front_gap_s=1.5),
        _car(5, 32.7, front_gap_s=18.5),
    )
    return state


def _tick(state: SessionState, cars: tuple[CarLap, ...], *, position: int = 3) -> object:
    state.cars_lap = cars
    state.position = position
    state.lap_distance = cars[state._player_idx].lap_distance
    state.delta_to_car_in_front_ms = cars[state._player_idx].delta_to_car_in_front_ms
    state._last_session_time = (state._last_session_time or 0.0) + 1.0
    state._last_update["lap_data"] = state._last_session_time
    return state.snapshot(state._last_session_time)


def test_stop_adjusted_gaps_and_neighbour_ties() -> None:
    from pitwall.state.race import stop_adjusted_gaps, true_neighbours

    cars = (
        _car(1, 0.0),
        _car(3, 10.0, penalties=2),
        _car(4, 12.0, front_gap_s=2.0, penalties=3),
    )
    assert stop_adjusted_gaps(cars, 1, TRACK_M, SPEED_MPS, (0, 1, 0), 22.0) == {
        0: -34.0,
        2: -19.0,
    }
    assert stop_adjusted_gaps(cars, 5, TRACK_M, SPEED_MPS, (0, 1, 0), 22.0) == {}

    neighbours = (
        SimpleNamespace(car_position=2),
        SimpleNamespace(car_position=4),
        SimpleNamespace(car_position=1),
        SimpleNamespace(car_position=5),
    )
    assert true_neighbours({0: 0.0, 1: 0.0, 2: -1.0, 3: 1.0}, neighbours, 3) == (0, 1)


def test_piastri_stays_true_rival_through_pit_and_stale_history() -> None:
    state = _session()
    before = state.snapshot(811.0)
    assert before.true_rival_behind_name == "PIASTRI"
    assert before.true_rival_behind_gap_s == pytest.approx(1.5)
    assert before.rival_behind_name == "PIASTRI"

    in_lane = (
        state.cars_lap[0],
        state.cars_lap[1],
        state.cars_lap[2],
        _car(5, 36.2, front_gap_s=22.0, pit_status=1),
        _car(4, 14.2, front_gap_s=1.5),
    )
    state._cars_pitted_this_lap.add(3)
    during = _tick(state, in_lane)
    assert during.true_rival_behind_name == "PIASTRI"
    assert during.true_rival_behind_gap_s == pytest.approx(1.5)
    assert during.true_rival_behind_pitted
    assert during.rival_behind_name == "NORRIS"

    exited_stale = tuple(
        replace(car, pit_status=0, num_pit_stops=1) if i == 3 else car
        for i, car in enumerate(in_lane)
    )
    after_exit_stale = _tick(state, exited_stale)
    assert after_exit_stale.true_rival_behind_name == "PIASTRI"
    assert after_exit_stale.true_rival_behind_gap_s == pytest.approx(1.5)

    state._histories[3] = _history(16, 17)
    after_exit = _tick(state, exited_stale)
    assert after_exit.true_rival_behind_name == "PIASTRI"
    assert after_exit.true_rival_behind_stops_owed == 0
    assert after_exit.true_rival_behind_gap_s == pytest.approx(1.5)
    assert after_exit.true_rival_behind_pitted


def test_player_stop_adjusts_piastri_to_true_behind() -> None:
    state = _session()
    state._histories[2] = _history(16, 17)
    cars = (
        _car(1, 0.0),
        _car(2, 10.0, front_gap_s=10.0),
        _car(5, 30.5, front_gap_s=20.5, num_pit_stops=1),
        _car(4, 10.0, front_gap_s=0.0),
        _car(3, 10.0, front_gap_s=20.5),
    )
    snapshot = _tick(state, cars, position=5)
    assert snapshot.true_rival_behind_name == "PIASTRI"
    assert snapshot.true_rival_behind_stops_owed == 1
    assert snapshot.true_rival_behind_gap_s == pytest.approx(1.5)


def test_pitted_physical_rival_does_not_replace_stop_adjusted_neighbour() -> None:
    from pitwall.engine import _true_behind_view
    from pitwall.strategy.pitwindow import RivalView

    state = _session()
    state._histories[3] = _history(16, 17)
    state._pitted_lap_snapshot.add(4)
    cars = (
        _car(1, 0.0),
        _car(2, 10.0, front_gap_s=10.0),
        _car(3, 12.7, front_gap_s=2.7),
        _car(5, 35.9, front_gap_s=22.0),
        _car(4, 13.9, front_gap_s=1.2, num_pit_stops=1),
    )
    snapshot = _tick(state, cars)
    assert snapshot.rival_behind_name == "NORRIS"
    assert snapshot.rival_behind_pitted
    assert snapshot.true_rival_behind_name == "PIASTRI"
    assert snapshot.true_rival_behind_gap_s == pytest.approx(1.2)

    physical = run(
        rival_behind=RivalView(
            snapshot.rival_behind_idx,
            snapshot.rival_behind_name,
            snapshot.rival_behind_pace_ms,
            snapshot.rival_behind_pitted,
        ),
        gap_behind_s=snapshot.gap_behind_s,
    )
    true_behind, true_gap = _true_behind_view(snapshot)
    assert true_behind is not None
    true_plan = run(
        rival_behind=true_behind,
        gap_behind_s=snapshot.gap_behind_s,
        rival_behind_gap_s=true_gap,
    )
    assert physical.plan == "cover"
    assert true_plan.plan not in {"cover", "defend"}


def test_lapped_car_is_not_a_true_rival() -> None:
    from pitwall.state.race import stop_adjusted_gaps, true_neighbours

    cars = (
        _car(3, 12.0),
        _car(4, 13.0, front_gap_s=1.0),
        replace(
            _car(5, 14.0, front_gap_s=1.0),
            total_distance=LEADER_DISTANCE - 12.0 * SPEED_MPS - TRACK_M,
        ),
    )
    rel = stop_adjusted_gaps(cars, 0, TRACK_M, SPEED_MPS, (0, 0, 0), 22.0)
    assert 2 not in rel
    assert true_neighbours(rel, cars, 3) == (-1, 1)


def test_safety_car_recomputes_true_rival_and_flashback_clears_map() -> None:
    state = _session()
    state._histories[1] = _history(16)
    state._histories[2] = _history(16, 17)
    state._model = ModelView(pit_loss_s=22.0, sc_pit_loss_s=8.0)
    cars = (
        _car(1, 0.0),
        _car(2, 10.0, front_gap_s=10.0),
        _car(3, 22.0, front_gap_s=12.0),
        _car(4, 40.0, front_gap_s=18.0),
        _car(5, 60.0, front_gap_s=20.0),
    )
    state.cars_lap = cars
    state.position = 3
    green = state.snapshot(811.0)
    assert green.true_rival_behind_idx == 1
    assert green.true_rival_behind_gap_s == pytest.approx(10.0)

    state.safety_car_status = 1
    under_sc = _tick(state, cars)
    assert under_sc.true_rival_ahead_idx == 1
    assert under_sc.true_rival_ahead_gap_s == pytest.approx(4.0)

    state._handle_rewind(800.0)
    assert state._true_rel == {}


def test_inter_tyre_without_history_owes_no_stop() -> None:
    state = _session()
    state._histories.pop(3)
    state.cars_status = (
        SimpleNamespace(visual_tyre_compound=16),
        SimpleNamespace(visual_tyre_compound=16),
        SimpleNamespace(visual_tyre_compound=17),
        SimpleNamespace(visual_tyre_compound=7),
    )

    assert state._stops_owed(3, state.cars_lap[3]) == 0


def test_sprint_without_history_owes_no_stop() -> None:
    state = _session()
    state.weekend_structure = (15, 16)
    state._histories.pop(3)
    car = replace(state.cars_lap[3], num_pit_stops=0)

    assert state._is_sprint()
    assert state._stops_owed(3, car) == 0


def test_true_rival_snapshot_defaults_and_payload_fields() -> None:
    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics
    from pitwall.server.app import state_payload
    from pitwall.state.session import Snapshot

    defaults = Snapshot(now=0.0)
    assert defaults.true_rival_ahead_idx == -1
    assert defaults.true_rival_behind_name == ""
    assert defaults.true_rival_behind_gap_s == math.inf

    snapshot = replace(
        defaults,
        true_rival_ahead_idx=2,
        true_rival_ahead_name="OCON",
        true_rival_ahead_gap_s=2.7,
        true_rival_ahead_stops_owed=1,
        true_rival_ahead_pitted=False,
        true_rival_behind_idx=3,
        true_rival_behind_name="PIASTRI",
        true_rival_behind_gap_s=1.5,
        true_rival_behind_stops_owed=0,
        true_rival_behind_pitted=True,
    )
    payload = state_payload(
        snapshot, settings=ConfigStore().current(), metrics=Metrics(), quiet=False
    )
    assert payload["race"]["true_rival_ahead"] == {
        "idx": 2,
        "name": "OCON",
        "gap_s": 2.7,
        "stops_owed": 1,
        "pitted": False,
    }
    assert payload["race"]["true_rival_behind"] == {
        "idx": 3,
        "name": "PIASTRI",
        "gap_s": 1.5,
        "stops_owed": 0,
        "pitted": True,
    }
