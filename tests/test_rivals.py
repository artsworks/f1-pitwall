"""Rival scope + pit-exit projection (docs/03 formula) and restricted detection."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.state.race import penalty_standing, relevant_rivals, second_rivals
from pitwall.state.session import SessionState

from .synth import pack_packet


@dataclass
class _Car:
    car_position: int
    lap_distance: float
    delta_to_car_in_front_ms: int = 0
    result_status: int = 2
    pit_status: int = 0
    total_distance: float | None = None


def test_ahead_behind_scope() -> None:
    cars = [
        _Car(3, 1000.0, 900),
        _Car(2, 1100.0, 500),
        _Car(4, 950.0, 1500),
        _Car(5, 900.0, 800),
    ]
    ahead, behind, _ = relevant_rivals(cars, 0, 2.0, (1000.0, 5000.0, 0.0))
    assert (ahead, behind) == (1, 2)
    _, behind, _ = relevant_rivals(cars, 0, 1.0, (1000.0, 5000.0, 0.0))
    assert behind == -1  # outside the configured gap


def test_pit_exit_projection_formula() -> None:
    # 5 km lap, player at 1000 m, pace 90 s, pit loss 22 s -> 5000*22/90 = 1222 m lost.
    lost = 5000.0 * 22.0 / 90.0
    d_target = (1000.0 - lost) % 5000.0  # 4777.8 m
    cars = [
        _Car(5, 1000.0),
        _Car(6, 4700.0),  # behind the target point
        _Car(7, 4800.0),  # nearest ahead of target -> pit-exit rival
        _Car(8, 200.0),
        _Car(9, 4790.0, pit_status=1),  # pitting cars excluded
    ]
    assert 4700.0 < d_target < 4800.0
    _, _, pit_exit = relevant_rivals(cars, 0, 2.0, (1000.0, 5000.0, lost))
    assert pit_exit == 2


def _lap_boundary(ingest: Ingest, lap: int, t: float, zero: bool) -> None:
    rival = {"fuel_in_tank": 0.0 if zero else 50.0, "ers_store_energy": 0.0 if zero else 1e6}
    ingest.on_datagram(
        pack_packet(
            PacketId.CAR_STATUS, {"cars": {0: {"fuel_in_tank": 60.0}, 1: rival}}, session_time=t
        ),
        t,
    )
    ingest.on_datagram(
        pack_packet(
            PacketId.CAR_DAMAGE,
            {"cars": {1: {"tyres_wear": [0.0] * 4 if zero else [10.0] * 4}}},
            session_time=t,
        ),
        t,
    )
    ingest.on_datagram(
        pack_packet(
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: {"current_lap_num": lap, "result_status": 2, "car_position": 1},
                    1: {"current_lap_num": lap, "result_status": 2, "car_position": 2},
                }
            },
            session_time=t,
        ),
        t,
    )


def test_restricted_detection_flips_and_clears() -> None:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    _lap_boundary(ingest, 1, 0.0, zero=True)
    for lap in (2, 3):
        _lap_boundary(ingest, lap, float(lap), zero=True)
    assert state.snapshot(3.0).rival_data_restricted
    _lap_boundary(ingest, 4, 4.0, zero=False)
    assert not state.snapshot(4.0).rival_data_restricted


@dataclass
class _Timed:
    car_position: int
    delta_to_race_leader_ms: int
    penalties: int = 0
    total_distance: float = 50000.0
    result_status: int = 2


def test_penalty_standing_drops_player_behind_cars_within_penalty() -> None:
    # Race 2 finish: P2 on the road with 8 s; cars behind at +1.4, +4.5, +7.2, +7.7, +15.9.
    cars = [
        _Timed(1, 0),
        _Timed(2, 850, penalties=8),
        _Timed(3, 2250),
        _Timed(4, 5350),
        _Timed(5, 8050),
        _Timed(6, 8550),
        _Timed(7, 16750),
    ]
    pos, margin, threat = penalty_standing(cars, 1, 5000.0)
    assert pos == 6
    assert threat == 2 and margin == pytest.approx(-6.6)


def test_penalty_standing_ignores_lapped_cars_and_clean_races() -> None:
    cars = [_Timed(1, 0), _Timed(2, 900, penalties=5), _Timed(3, 1100, total_distance=44000.0)]
    assert penalty_standing(cars, 1, 5000.0)[:2] == (2, math.inf)
    # Crossing the line: the car just behind is still on the previous lap.
    line = [_Timed(1, 0), _Timed(2, 900, penalties=5), _Timed(3, 1300, total_distance=49990.0)]
    assert penalty_standing(line, 1, 5000.0)[0] == 3
    clean = [_Timed(1, 0), _Timed(2, 900), _Timed(3, 1100)]
    assert penalty_standing(clean, 1, 5000.0) == (2, math.inf, -1)
    covered = [_Timed(1, 0), _Timed(2, 900, penalties=5), _Timed(3, 6900)]
    pos, margin, _ = penalty_standing(covered, 1, 5000.0)
    assert pos == 2 and margin == pytest.approx(1.0)


def test_penalty_standing_skips_a_lapped_car_without_stopping() -> None:
    cars = [
        _Timed(1, 0, total_distance=50000.0),
        _Timed(2, 900, penalties=5, total_distance=50000.0),
        _Timed(3, 1100, total_distance=44000.0),
        _Timed(4, 3000, total_distance=50001.0),
    ]
    assert penalty_standing(cars, 1, 5000.0)[0] == 3


def test_second_rivals_scope() -> None:
    cars = [
        _Car(1, 1200.0, 400),
        _Car(2, 1100.0, 500),
        _Car(3, 1000.0, 900),
        _Car(4, 950.0, 1500),
        _Car(5, 900.0, 700),
        _Car(6, 800.0, 800),
    ]
    # Player P3 (idx 2): ahead is P2, behind is P4 -> ahead2 P1, behind2 P5.
    assert second_rivals(cars, 1, 3) == (0, 4)
    # Player P2 (idx 1): nobody ahead of P1.
    assert second_rivals(cars, 0, 2) == (-1, 3)
    # Missing immediate rivals give -1 on that side only.
    assert second_rivals(cars, -1, 3) == (-1, 4)
    assert second_rivals(cars, 1, -1) == (0, -1)
    assert second_rivals(cars, -1, -1) == (-1, -1)
    cars[0].pit_status = 1
    assert second_rivals(cars, 1, 3) == (-1, 4)
    cars[0].pit_status = 0
    cars[0].result_status = 3
    assert second_rivals(cars, 1, 3) == (-1, 4)


def test_second_rivals_fill_in_snapshot() -> None:
    from pitwall.state.session import Participant

    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    ingest.on_datagram(
        pack_packet(
            PacketId.SESSION,
            {"session_type": 15, "track_length": 5000, "total_laps": 20},
        ),
        0.0,
    )
    state.participants = tuple(Participant(name=n) for n in ("ME", "P2", "P4", "P1", "P5"))
    cars = {
        0: {
            "current_lap_num": 5,
            "car_position": 3,
            "result_status": 2,
            "driver_status": 4,
            "delta_to_car_in_front_ms_part": 800,
        },
        1: {
            "current_lap_num": 5,
            "car_position": 2,
            "result_status": 2,
            "delta_to_car_in_front_ms_part": 400,
        },
        2: {
            "current_lap_num": 5,
            "car_position": 4,
            "result_status": 2,
            "delta_to_car_in_front_ms_part": 1500,
        },
        3: {"current_lap_num": 5, "car_position": 1, "result_status": 2},
        4: {
            "current_lap_num": 5,
            "car_position": 5,
            "result_status": 2,
            "delta_to_car_in_front_ms_part": 700,
        },
    }
    ingest.on_datagram(pack_packet(PacketId.LAP_DATA, {"cars": cars}, session_time=1.0), 1.0)
    snap = state.snapshot(1.0)
    assert snap.rival_ahead2_pos == 1 and snap.rival_ahead2_name == "P1"
    assert snap.rival_ahead2_gap_s == pytest.approx(0.8 + 0.4)
    assert snap.rival_behind2_pos == 5 and snap.rival_behind2_name == "P5"
    assert snap.rival_behind2_gap_s == pytest.approx(1.5 + 0.7)


def test_behind_rival_rejects_lapped_and_implausible_gap() -> None:
    cars = [
        _Car(3, 1000.0, 900, total_distance=50000.0),
        _Car(2, 1200.0, 800, total_distance=50200.0),
        _Car(4, 950.0, 1500, total_distance=44000.0),
    ]
    _, behind, _ = relevant_rivals(cars, 0, math.inf, (1000.0, 5000.0, 0.0), 70.0)
    assert behind == -1

    cars[2].total_distance = 49950.0
    cars[2].delta_to_car_in_front_ms = 65503
    _, behind, _ = relevant_rivals(cars, 0, math.inf, (1000.0, 5000.0, 0.0), 70.0)
    assert behind == -1
