"""Rival scope + pit-exit projection (docs/03 formula) and restricted detection."""

from __future__ import annotations

from dataclasses import dataclass

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.state.race import relevant_rivals
from pitwall.state.session import SessionState

from .synth import pack_packet


@dataclass
class _Car:
    car_position: int
    lap_distance: float
    delta_to_car_in_front_ms: int = 0
    result_status: int = 2
    pit_status: int = 0


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
