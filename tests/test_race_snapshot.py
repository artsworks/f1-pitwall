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


def test_player_pit_stop_count() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0, num_pit_stops=1)
    assert state.snapshot(1.0).num_pit_stops == 1


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


def test_pena_warning_is_not_a_penalty() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    for t, fields in (
        (2.0, (5, 27, 0, 255, 255, 5, 0)),  # track-limit warning: time_s is a 255 sentinel
        (3.0, (4, 21, 0, 255, 3, 5, 0)),  # the 3 s time penalty for repeated warnings
    ):
        pena = pack_packet(
            PacketId.EVENT,
            {
                "event_string_code": b"PENA",
                "event_data": struct.pack("<BBBBBBB", *fields).ljust(12, b"\0"),
            },
            session_time=t,
        )
        ingest.on_datagram(pena, t)
        _lap(ingest, t + 0.1)
        snap = state.snapshot(t + 0.1)
        if t == 2.0:
            assert not snap.penalty_recent and snap.penalty_time_s == 0
    assert snap.penalty_recent and snap.penalty_kind == "time" and snap.penalty_time_s == 3


def test_pena_track_limit_warning_kind() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    assert not state.snapshot(1.0).track_warning_recent
    pena = pack_packet(
        PacketId.EVENT,
        {
            "event_string_code": b"PENA",
            "event_data": struct.pack("<BBBBBBB", 5, 28, 0, 255, 255, 5, 0).ljust(12, b"\0"),
        },
        session_time=2.0,
    )
    ingest.on_datagram(pena, 2.0)
    _lap(ingest, 2.1)
    snap = state.snapshot(2.1)
    assert snap.track_warning_recent and snap.track_warning_kind == "significant"
    assert not snap.penalty_recent
    _lap(ingest, 30.0)
    assert not state.snapshot(30.0).track_warning_recent


def _pena(ingest: Ingest, t: float, *fields: int) -> None:
    pena = pack_packet(
        PacketId.EVENT,
        {
            "event_string_code": b"PENA",
            "event_data": struct.pack("<BBBBBBB", *fields).ljust(12, b"\0"),
        },
        session_time=t,
    )
    ingest.on_datagram(pena, t)


def test_corner_cut_and_track_limit_warnings_count_separately() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    _pena(ingest, 2.0, 5, 27, 0, 255, 255, 5, 0)
    _pena(ingest, 3.0, 5, 28, 0, 255, 255, 5, 0)
    snap = state.snapshot(3.0)
    assert snap.track_warning_kind == "significant" and snap.track_warning_count == 2
    _pena(ingest, 4.0, 5, 7, 0, 255, 255, 5, 0)
    snap = state.snapshot(4.0)
    assert snap.track_warning_kind == "cut" and snap.track_warning_count == 1
    _pena(ingest, 5.0, 5, 29, 0, 255, 255, 5, 0)
    assert state.snapshot(5.0).track_warning_count == 3


def test_penalty_total_includes_new_penalty_before_lap_data_catches_up() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0, penalties=3)
    _pena(ingest, 2.0, 4, 7, 0, 255, 10, 5, 0)
    assert state.snapshot(2.0).penalty_s == 13
    _lap(ingest, 2.1, penalties=13)
    assert state.snapshot(2.1).penalty_s == 13
    _lap(ingest, 60.0, penalties=0)  # served at the stop
    assert state.snapshot(60.0).penalty_s == 0


def test_flashback_removes_future_warning_counts_and_pending_penalty() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    _pena(ingest, 2.0, 5, 27, 0, 255, 255, 5, 0)
    _pena(ingest, 3.0, 5, 7, 0, 255, 255, 5, 0)
    _pena(ingest, 4.0, 5, 28, 0, 255, 255, 5, 0)
    _pena(ingest, 5.0, 4, 7, 0, 255, 10, 5, 0)
    _lap(ingest, 2.5, penalties=0)
    snap = state.snapshot(2.5)
    assert snap.track_warning_kind == "minor"
    assert snap.track_warning_count == 1
    assert snap.penalty_s == 0 and not snap.penalty_recent
    _pena(ingest, 3.0, 5, 7, 0, 255, 255, 5, 0)
    assert state.snapshot(3.0).track_warning_count == 1


def test_penalty_total_does_not_double_count_lap_data_arriving_first() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0, penalties=3)
    _lap(ingest, 2.0, penalties=13)
    _pena(ingest, 2.0, 4, 7, 0, 255, 10, 5, 0)
    assert state.snapshot(2.0).penalty_s == 13
    _lap(ingest, 2.1, penalties=13)
    assert state.snapshot(2.1).penalty_s == 13


def test_weather_crossover_ignores_forecast_after_the_flag() -> None:
    ingest, state = _state()
    state._best_laps[0] = 90_000
    state.lap_num = 15  # 6 laps x 1:30 = 9 minutes left of a 20-lap race
    _forecast(ingest, 1.0, 6, 70)
    snap = state.snapshot(1.0)
    assert snap.weather_crossover == ""
    assert (snap.weather_crossover_pct, snap.weather_crossover_min) == (6, 10)
    assert snap.rain_pct_in_30 == 70


def test_grid_penalty_places_from_pena_event() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    _pena(ingest, 2.0, 2, 7, 0, 255, 0, 5, 5)  # type 2, places_gained 5
    snap = state.snapshot(2.0)
    assert snap.grid_penalty_places == 5 and snap.grid_penalty_recent
    _lap(ingest, 30.0)
    snap = state.snapshot(30.0)
    assert snap.grid_penalty_places == 5 and not snap.grid_penalty_recent


def test_grid_penalty_from_another_car_is_ignored() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0)
    _pena(ingest, 2.0, 2, 7, 1, 255, 0, 5, 5)  # vehicle_idx 1, not the player
    snap = state.snapshot(2.0)
    assert snap.grid_penalty_places == 0 and not snap.grid_penalty_recent


def test_positions_gained_uses_post_penalty_grid() -> None:
    ingest, state = _state()
    _lap(ingest, 1.0, grid_position=15, car_position=17)
    snap = state.snapshot(1.0)
    assert snap.grid_position == 15 and snap.positions_gained == -2
