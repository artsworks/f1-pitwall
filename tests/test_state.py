from __future__ import annotations

import pytest

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.state.ema import Ema
from pitwall.state.session import SessionState

from .synth import pack_packet


def _state() -> tuple[Ingest, SessionState]:
    ingest = Ingest()
    state = SessionState(ema_fast_s=3.0, ema_slow_s=30.0)
    state.register(ingest)
    return ingest, state


def _send(ingest: Ingest, pkt: bytes, t: float) -> None:
    ingest.on_datagram(pkt, t)


def test_phase_mapping() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(PacketId.LAP_DATA, {"cars": {0: {"driver_status": 3, "pit_status": 0}}}),
        0.0,
    )
    assert state.snapshot(0.0).phase == "out_lap"
    _send(
        ingest,
        pack_packet(PacketId.LAP_DATA, {"cars": {0: {"driver_status": 1, "pit_status": 1}}}),
        1.0,
    )
    assert state.snapshot(1.0).phase == "pitting"
    # F1 26 reports pit_status=1 while parked in the garage
    _send(
        ingest,
        pack_packet(PacketId.LAP_DATA, {"cars": {0: {"driver_status": 0, "pit_status": 1}}}),
        2.0,
    )
    assert state.snapshot(2.0).phase == "garage"


def test_ema_converges_and_uses_session_time() -> None:
    ingest, state = _state()
    # settle at 60, then step to 90: fast EMA (tau 3s) tracks far ahead of
    # slow EMA (tau 30s).
    for i in range(330):
        t = i / 30.0
        temp = 60 if t < 1.0 else 90
        _send(
            ingest,
            pack_packet(
                PacketId.CAR_TELEMETRY,
                {"cars": {0: {"tyres_inner_temperature": (temp, temp, temp, temp)}}},
                session_time=t,
            ),
            t,
        )
    snap = state.snapshot(t)
    assert snap.tyre_inner_ema_fast.FL > 88.0
    assert snap.tyre_inner_ema_slow.FL < 80.0


def test_ema_reset_on_session_time_rewind() -> None:
    ingest, state = _state()
    for i in range(10):
        t = 10.0 + i
        _send(
            ingest,
            pack_packet(
                PacketId.CAR_TELEMETRY,
                {"cars": {0: {"tyres_inner_temperature": (90, 90, 90, 90)}}},
                session_time=t,
            ),
            t,
        )
    # session_time jumps backwards > 1 s (flashback)
    _send(
        ingest,
        pack_packet(
            PacketId.CAR_TELEMETRY,
            {"cars": {0: {"tyres_inner_temperature": (50, 50, 50, 50)}}},
            session_time=5.0,
        ),
        20.0,
    )
    snap = state.snapshot(20.0)
    assert snap.tyre_inner_ema_fast.FL == pytest.approx(50.0)


def test_snapshot_fields_and_age() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.SESSION,
            {"session_type": 15, "track_id": 7, "total_laps": 50},
            session_time=10.0,
        ),
        0.0,
    )
    _send(
        ingest,
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"current_lap_num": 3, "driver_status": 1, "car_position": 2}}},
            session_time=10.5,
        ),
        0.1,
    )
    snap = state.snapshot(1.0)
    assert snap.session_kind == "race"
    assert snap.track_id == 7
    assert snap.total_laps == 50
    assert snap.lap_num == 3
    assert snap.position == 2
    assert snap.session_time == 10.5
    assert snap.age("lap_data") == pytest.approx(0.0)
    assert snap.age("car_damage") == float("inf")


def test_lap_summary_and_validity() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.SESSION, {"session_type": 15, "safety_car_status": 0}, session_time=0.0
        ),
        0.0,
    )

    def lap(num: int, t: float, **kw: object) -> None:
        car = {"current_lap_num": num, "driver_status": 1, "pit_status": 0, **kw}
        _send(ingest, pack_packet(PacketId.LAP_DATA, {"cars": {0: car}}, session_time=t), t)

    # lap 1 in progress, then lap 2 starts -> lap 1 summary (first_lap invalid)
    lap(1, 0.1, last_lap_time_ms=0)
    lap(2, 90.0, last_lap_time_ms=80_000, sector1_time_ms_part=25_000)
    assert len(state.laps) == 1
    s1 = state.laps[0]
    assert s1.lap_num == 1
    assert not s1.valid
    assert "first_lap" in s1.invalid_reasons
    assert s1.lap_time_ms == 80_000
    assert s1.sector1_ms == 25_000

    # clean lap 2 -> valid summary at lap 3 boundary
    lap(3, 180.0, last_lap_time_ms=81_000)
    assert state.laps[-1].lap_num == 2
    assert state.laps[-1].valid

    # lap 3 pitted -> invalid with 'pitted'
    lap(4, 200.0, last_lap_time_ms=82_000)
    # pit during lap 4 then cross to 5
    lap(4, 201.0, pit_status=1)
    lap(5, 270.0, last_lap_time_ms=83_000)
    assert not state.laps[-1].valid
    assert "pitted" in state.laps[-1].invalid_reasons


def test_ema_scalar() -> None:
    e = Ema(3.0)
    e.update(0.0, 100.0)
    assert e.value == 100.0
    e.update(3.0, 0.0)  # dt == tau -> retains e^-1 of the old value
    assert e.value == pytest.approx(100.0 * 0.3678794411, abs=1.0)


def test_car_damage_reaches_snapshot_and_payload() -> None:
    from pitwall.server.app import state_payload

    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.CAR_DAMAGE,
            {"cars": {0: {"front_left_wing_damage": 25, "floor_damage": 8}}},
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert snap.damage.front_left_wing == 25
    assert snap.damage.floor == 8

    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics

    payload = state_payload(snap, settings=ConfigStore().current(), metrics=Metrics(), quiet=False)
    assert payload["damage"]["front_left_wing"] == 25
    assert payload["damage"]["ers_fault"] == 0


def test_session_context_fields() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.SESSION,
            {
                "session_type": 5,
                "session_time_left": 900,
                "session_duration": 1200,
                "track_length": 5300,
                "pit_speed_limit": 80,
                "game_paused": 1,
            },
            session_time=5.0,
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert snap.session_kind == "qualifying"
    assert snap.session_time_left == 900.0
    assert snap.session_duration == 1200.0
    assert snap.track_length_m == 5300.0
    assert snap.pit_speed_limit == 80
    assert snap.game_paused
    assert snap.paused


def test_paused_from_network_paused() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(PacketId.CAR_STATUS, {"cars": {0: {"network_paused": 1}}}),
        0.0,
    )
    assert state.snapshot(0.0).paused


def test_participants_reach_snapshot() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.PARTICIPANTS,
            {
                "num_active_cars": 20,
                "cars": {0: {"name": b"VERSTAPPEN", "team_id": 1, "race_number": 1}},
            },
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert snap.num_active_cars == 20
    assert len(snap.participants) == 24
    assert snap.participants[0].name == "VERSTAPPEN"
    assert snap.participants[0].race_number == 1


def test_cars_lap_all_slots() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: {"lap_distance": 100.0, "current_lap_time_ms": 12_345, "sector": 1},
                    5: {"car_position": 7, "driver_status": 1, "result_status": 2},
                }
            },
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert len(snap.cars) == 24
    assert snap.cars[0].lap_distance == 100.0
    assert snap.cars[0].current_lap_time_ms == 12_345
    assert snap.cars[5].car_position == 7
    assert snap.current_lap_time_ms == 12_345


def test_session_history_best_laps() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.SESSION_HISTORY,
            {
                "car_idx": 0,
                "num_laps": 3,
                "laps": {
                    0: {"lap_time_ms": 95_000, "lap_valid_bit_flags": 0x01},
                    1: {"lap_time_ms": 90_000, "lap_valid_bit_flags": 0x00},  # invalid
                    2: {
                        "lap_time_ms": 93_000,
                        "lap_valid_bit_flags": 0x0F,
                        "sector1_ms_part": 28_000,
                        "sector2_ms_part": 30_000,
                        "sector3_ms_part": 35_000,
                    },
                },
            },
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert len(snap.field_best_laps) == 24
    assert snap.field_best_laps[0] == 93_000
    assert snap.player_best_lap_ms == 93_000
    assert snap.player_best_s1_ms == 28_000
    assert snap.player_best_s2_ms == 30_000
    assert snap.player_best_s3_ms == 35_000
    assert snap.player_laps_completed == 2


def test_tyre_sets_snapshot() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.TYRE_SETS,
            {
                "car_idx": 0,
                "sets": {
                    0: {
                        "visual_tyre_compound": 16,
                        "available": 1,
                        "fitted": 1,
                        "life_span": 20,
                        "usable_life": 25,
                    },
                    1: {"visual_tyre_compound": 16, "available": 1, "wear": 0},
                    2: {"visual_tyre_compound": 17, "available": 1, "wear": 40},
                    3: {"visual_tyre_compound": 18, "available": 1},
                },
                "fitted_idx": 0,
            },
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert len(snap.tyre_sets) == 20
    assert snap.tyre_sets[0].fitted == 1
    assert snap.fresh_sets_soft == 2
    assert snap.fresh_sets_medium == 0  # worn
    assert snap.fresh_sets_hard == 1
    assert snap.fresh_sets_current == 2
    assert snap.fitted_life_span == 20


def test_car_setups_and_telemetry_2() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.CAR_SETUPS,
            {"cars": {0: {"fuel_load": 35.0, "front_wing": 11, "rear_wing": 8, "brake_bias": 55}}},
        ),
        0.0,
    )
    _send(
        ingest,
        pack_packet(
            PacketId.CAR_TELEMETRY_2,
            {
                "cars": {
                    0: {
                        "active_aero_mode": 1,
                        "active_aero_available": 1,
                        "overtake_available": 1,
                        "overtake_active": 0,
                        "overtake_activation_distance": 400,
                        "driving_wrong_way": 1,
                    }
                }
            },
        ),
        0.1,
    )
    snap = state.snapshot(0.2)
    assert snap.setup_fuel_load == 35.0
    assert snap.setup_front_wing == 11
    assert snap.setup_rear_wing == 8
    assert snap.setup_brake_bias == 55
    assert snap.active_aero_mode == 1
    assert snap.overtake_available == 1
    assert snap.overtake_activation_distance_m == 400
    assert snap.driving_wrong_way


def test_red_flag_set_and_cleared() -> None:
    from .synth import make_event_packet

    ingest, state = _state()
    _send(ingest, make_event_packet(b"RDFL", session_time=1.0), 0.0)
    snap = state.snapshot(0.0)
    assert snap.red_flag
    assert snap.phase == "red_flag"
    _send(ingest, make_event_packet(b"SSTA", session_time=2.0), 0.1)
    assert not state.snapshot(0.1).red_flag

    # SEND marks the session ended
    _send(ingest, make_event_packet(b"SEND", session_time=3.0), 0.2)
    assert state.snapshot(0.2).session_ended


def test_session_uid_change_resets_state() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"current_lap_num": 5, "driver_status": 1, "car_position": 2}}},
        ),
        0.0,
    )
    _send(ingest, pack_packet(PacketId.EVENT, {"event_string_code": b"RDFL"}), 0.1)
    snap = state.snapshot(0.1)
    assert snap.lap_num == 5
    assert snap.red_flag

    # New session: everything per-session resets.
    _send(
        ingest,
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"current_lap_num": 1, "driver_status": 0}}},
            session_uid=0x1234,
        ),
        0.2,
    )
    snap = state.snapshot(0.2)
    assert snap.session_uid == 0x1234
    assert snap.lap_num == 1
    assert not snap.red_flag
    assert not snap.session_ended
    assert snap.rewinds == 0
