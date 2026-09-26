"""Race facts in the snapshot, driven through real packet layouts."""

from __future__ import annotations

import struct

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.state.model_view import ModelView
from pitwall.state.session import SessionState

from .synth import pack_packet


def _state() -> tuple[Ingest, SessionState]:
    ingest = Ingest()
    state = SessionState(ema_fast_s=3.0, ema_slow_s=30.0)
    state.register(ingest)
    ingest.on_datagram(
        pack_packet(PacketId.SESSION, {"session_type": 15, "track_length": 5000, "total_laps": 20}),
        0.0,
    )
    return ingest, state


def _lap(ingest: Ingest, t: float, **player: object) -> None:
    cars: dict[int, dict[str, object]] = {
        0: {"current_lap_num": 5, "car_position": 3, "result_status": 2, "driver_status": 4},
        1: {"current_lap_num": 5, "car_position": 2, "result_status": 2},
        2: {
            "current_lap_num": 5,
            "car_position": 4,
            "result_status": 2,
            "delta_to_car_in_front_ms_part": 1500,
        },
    }
    cars[0].update(player)
    ingest.on_datagram(pack_packet(PacketId.LAP_DATA, {"cars": cars}, session_time=t), t)


def test_gaps_and_laps_remaining() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0, delta_to_car_in_front_ms_part=800, delta_to_car_in_front_minutes_part=0)
    snap = state.snapshot(1.0)
    assert snap.gap_ahead_s == 0.8
    assert snap.gap_behind_s == 1.5
    assert snap.laps_remaining == 16


def test_penalty_recent_from_pena_event() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    detail = struct.pack("<BBBBBBB", 4, 7, 0, 255, 5, 5, 0)
    pena = pack_packet(
        PacketId.EVENT,
        {"event_string_code": b"PENA", "event_data": detail.ljust(12, b"\0")},
        session_time=2.0,
    )
    ingest.on_datagram(pena, 2.0)
    _lap(ingest, 3.0)
    snap = state.snapshot(3.0)
    assert snap.penalty_recent
    assert snap.penalty_type == 4
    _lap(ingest, 30.0)
    assert not state.snapshot(30.0).penalty_recent


def test_blue_flag() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    ingest.on_datagram(
        pack_packet(PacketId.CAR_STATUS, {"cars": {0: {"vehicle_fia_flags": 4}}}, session_time=1.1),
        1.1,
    )
    assert state.snapshot(1.1).blue_flag


def _forecast(ingest: Ingest, t: float, rain10: int, rain30: int) -> None:
    samples = {
        0: {"session_type": 15, "time_offset": 0, "rain_percentage": 0},
        1: {"session_type": 15, "time_offset": 10, "rain_percentage": rain10},
        2: {"session_type": 15, "time_offset": 30, "rain_percentage": rain30},
    }
    ingest.on_datagram(
        pack_packet(
            PacketId.SESSION,
            {
                "session_type": 15,
                "track_length": 5000,
                "total_laps": 20,
                "num_weather_forecast_samples": 3,
                "weather_forecast_samples": samples,
            },
            session_time=t,
        ),
        t,
    )


def test_weather_crossover_with_hysteresis() -> None:
    ingest, state = _state()
    _forecast(ingest, 1.0, 40, 62)
    assert state.snapshot(1.0).weather_crossover == ""  # 62 < 60 + 5 hysteresis
    _forecast(ingest, 2.0, 40, 70)
    assert state.snapshot(2.0).weather_crossover == "to_inter"
    _forecast(ingest, 3.0, 40, 58)
    assert state.snapshot(3.0).weather_crossover == "to_inter"  # holds inside band
    _forecast(ingest, 4.0, 20, 50)
    assert state.snapshot(4.0).weather_crossover == ""


def test_model_view_propagates() -> None:
    _ingest, state = _state()
    state.set_model(ModelView(deg_fit_source="fit", laps_of_pace=7.5, pit_loss_s=21.0))
    snap = state.snapshot(1.0)
    assert snap.deg_fit_source == "fit"
    assert snap.laps_of_pace == 7.5
    assert snap.pit_loss_s == 21.0


def test_drs_available_needs_gap_and_permission() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0, delta_to_car_in_front_ms_part=600)
    ingest.on_datagram(
        pack_packet(PacketId.CAR_STATUS, {"cars": {0: {"drs_allowed": 1}}}, session_time=1.1), 1.1
    )
    assert state.snapshot(1.1).drs_available
    ingest.on_datagram(
        pack_packet(
            PacketId.SESSION,
            {"session_type": 15, "track_length": 5000, "safety_car_status": 1},
            session_time=1.2,
        ),
        1.2,
    )
    assert not state.snapshot(1.2).drs_available
