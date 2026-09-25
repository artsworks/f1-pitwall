from __future__ import annotations

import struct

from pitwall.protocol.header import PACKET_SIZES, PacketId, parse_header
from pitwall.protocol.layouts import Corners
from pitwall.protocol.packets import (
    CarDamagePacket,
    CarStatusPacket,
    CarTelemetryPacket,
    EventPacket,
    LapDataPacket,
    SessionPacket,
    car_field_offset,
    parse,
)

from .synth import pack_packet


def test_session_parse() -> None:
    pkt = pack_packet(
        PacketId.SESSION,
        {
            "session_type": 15,
            "track_id": 7,
            "total_laps": 52,
            "safety_car_status": 1,
            "track_length": 5000,
            "weekend_structure": (1, 2, 15),
        },
    )
    p = parse(PacketId.SESSION, pkt)
    assert isinstance(p, SessionPacket)
    assert p.session_type == 15
    assert p.track_id == 7
    assert p.total_laps == 52
    assert p.safety_car_status == 1
    assert p.track_length == 5000
    assert p.weekend_structure[:3] == (1, 2, 15)
    assert len(p.marshal_zones) == 21
    assert len(p.weather_forecast_samples) == 64
    assert len(p.drs_zones) == 4


def test_lap_data_parse_and_split_times() -> None:
    pkt = pack_packet(
        PacketId.LAP_DATA,
        {
            "cars": {
                0: {
                    "current_lap_num": 7,
                    "car_position": 3,
                    "lap_distance": 1234.5,
                    "sector": 1,
                    "driver_status": 1,
                    "pit_status": 0,
                    "sector1_time_ms_part": 23_456,
                    "sector1_time_minutes_part": 1,
                    "sector2_time_ms_part": 40_000,
                    "delta_to_car_in_front_ms_part": 1_234,
                },
                5: {"current_lap_num": 9, "driver_status": 2},
            },
            "time_trial_pb_car_idx": 7,
        },
    )
    p = parse(PacketId.LAP_DATA, pkt)
    assert isinstance(p, LapDataPacket)
    assert len(p.cars) == 24
    car0 = p.cars[0]
    assert car0.current_lap_num == 7
    assert car0.lap_distance == 1234.5
    assert car0.sector1_ms == 1 * 60000 + 23_456  # 1:23.456 -> 83456
    assert car0.sector2_ms == 40_000
    assert car0.delta_to_car_in_front_ms == 1_234
    assert p.cars[5].current_lap_num == 9
    assert p.time_trial_pb_car_idx == 7


def test_car_telemetry_corners_order() -> None:
    # Wire order RL, RR, FL, FR.
    pkt = pack_packet(
        PacketId.CAR_TELEMETRY,
        {
            "cars": {
                0: {
                    "speed": 300,
                    "tyres_inner_temperature": (91, 92, 93, 94),
                    "brakes_temperature": (500, 510, 520, 530),
                    "drs": 1,
                }
            }
        },
    )
    p = parse(PacketId.CAR_TELEMETRY, pkt)
    assert isinstance(p, CarTelemetryPacket)
    car = p.cars[0]
    assert car.speed == 300
    assert isinstance(car.tyres_inner_temperature, Corners)
    assert car.tyres_inner_temperature.rl == 91
    assert car.tyres_inner_temperature.rr == 92
    assert car.tyres_inner_temperature.fl == 93
    assert car.tyres_inner_temperature.fr == 94
    assert car.tyres_inner_temperature.FL == 93
    assert car.brakes_temperature.FR == 530


def test_car_status_parse() -> None:
    pkt = pack_packet(
        PacketId.CAR_STATUS,
        {
            "cars": {
                0: {
                    "fuel_in_tank": 30.5,
                    "fuel_remaining_laps": 10.25,
                    "actual_tyre_compound": 18,
                    "visual_tyre_compound": 17,
                    "tyres_age_laps": 12,
                    "ers_deploy_mode": 2,
                    "drs_allowed": 1,
                    "ers_store_energy": 2_000_000.0,
                }
            }
        },
    )
    p = parse(PacketId.CAR_STATUS, pkt)
    assert isinstance(p, CarStatusPacket)
    car = p.cars[0]
    assert car.actual_tyre_compound == 18
    assert car.visual_tyre_compound == 17
    assert car.fuel_remaining_laps == 10.25
    assert car.ers_deploy_mode == 2


def test_car_damage_parse() -> None:
    pkt = pack_packet(
        PacketId.CAR_DAMAGE,
        {"cars": {0: {"tyres_wear": (10.0, 11.0, 22.5, 13.0), "gearbox_damage": 7}}},
    )
    p = parse(PacketId.CAR_DAMAGE, pkt)
    assert isinstance(p, CarDamagePacket)
    assert p.cars[0].tyres_wear.FL == 22.5
    assert p.cars[0].gearbox_damage == 7


def test_event_code_and_detail() -> None:
    flbk = pack_packet(
        PacketId.EVENT,
        {
            "event_string_code": b"FLBK",
            "event_data": struct.pack("<If", 1234, 5.0).ljust(12, b"\0"),
        },
    )
    p = parse(PacketId.EVENT, flbk)
    assert isinstance(p, EventPacket)
    assert p.code == "FLBK"
    assert p.detail["flashback_frame_identifier"] == 1234

    scar = pack_packet(
        PacketId.EVENT,
        {"event_string_code": b"SCAR", "event_data": struct.pack("<BB", 1, 0).ljust(12, b"\0")},
    )
    p = parse(PacketId.EVENT, scar)
    assert p.detail == {"safety_car_type": 1, "event_type": 0}


def test_car_field_offset() -> None:
    pkt = pack_packet(PacketId.LAP_DATA, {"cars": {3: {"current_lap_num": 42}}})
    off = car_field_offset(PacketId.LAP_DATA, 3, "current_lap_num")
    assert pkt[off] == 42


def test_parse_header_passthrough() -> None:
    pkt = pack_packet(PacketId.SESSION, {"total_laps": 44})
    h = parse_header(pkt)
    p = parse(PacketId.SESSION, pkt, h)
    assert p.header is h
    assert len(pkt) == PACKET_SIZES[PacketId.SESSION]
