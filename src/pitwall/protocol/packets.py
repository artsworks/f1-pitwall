"""Parsed packet objects, generated from the declarative layouts.

Each packet class is slotted: `header` plus one attribute per layout field.
Car arrays are tuples of slotted per-car objects; 4-element tyre arrays are
`Corners` (wire order RL, RR, FL, FR — `tyre_inner.FL` works).

Import-time gate: for every table, HEADER_SIZE + body size must equal
PACKET_SIZES[id]. A mismatch means the table (or header layout) is wrong and
fails loudly here rather than mis-parsing later.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import field as dc_field
from dataclasses import make_dataclass
from typing import Any

from pitwall.protocol.header import (
    HEADER_SIZE,
    PACKET_SIZES,
    PacketHeader,
    PacketId,
    parse_header,
)
from pitwall.protocol.layouts import (
    ACTIVE_AERO_ZONE,
    CAR_DAMAGE_CAR,
    CAR_DAMAGE_LAYOUT,
    CAR_SETUP_CAR,
    CAR_SETUPS_LAYOUT,
    CAR_STATUS_CAR,
    CAR_STATUS_LAYOUT,
    CAR_TELEMETRY_2_CAR,
    CAR_TELEMETRY_2_LAYOUT,
    CAR_TELEMETRY_CAR,
    CAR_TELEMETRY_LAYOUT,
    EVENT_LAYOUT,
    LAP_DATA_CAR,
    LAP_DATA_LAYOUT,
    LAP_HISTORY,
    MARSHAL_ZONE,
    MOTION_EX_LAYOUT,
    PARTICIPANT_CAR,
    PARTICIPANTS_LAYOUT,
    SESSION_HISTORY_LAYOUT,
    SESSION_LAYOUT,
    TYRE_SET,
    TYRE_SETS_LAYOUT,
    TYRE_STINT_HISTORY,
    WEATHER_FORECAST_SAMPLE,
    Array,
    CompiledLayout,
    Corners,
    Field,
    Item,
    compile_layout,
)


def _class_for(
    name: str,
    layout: tuple[Item, ...],
    extra: tuple[str, ...] = (),
    *,
    header: bool = True,
) -> type:
    """Slotted data class: `header` (packets only) plus one attribute per
    layout item (plus extras)."""
    return make_dataclass(
        name,
        ([("header", PacketHeader)] if header else [])
        + [(item.name, Any) for item in layout]
        + [(e, Any, dc_field(default=None)) for e in extra],
        slots=True,
    )


# Slotted classes for every (sub-)layout -----------------------------------

# Generated classes are Any-typed: mypy cannot use dataclasses created at
# runtime as static types, and the fields are data-driven anyway.
MarshalZone: Any = _class_for("MarshalZone", MARSHAL_ZONE, header=False)
WeatherForecastSample: Any = _class_for(
    "WeatherForecastSample", WEATHER_FORECAST_SAMPLE, header=False
)
Zone: Any = _class_for("Zone", ACTIVE_AERO_ZONE, header=False)

_LAP_CAR_EXTRA = (
    "sector1_ms",
    "sector2_ms",
    "delta_to_car_in_front_ms",
    "delta_to_race_leader_ms",
)
LapDataCar: Any = _class_for("LapDataCar", LAP_DATA_CAR, _LAP_CAR_EXTRA, header=False)
CarTelemetry: Any = _class_for("CarTelemetry", CAR_TELEMETRY_CAR, header=False)
CarStatus: Any = _class_for("CarStatus", CAR_STATUS_CAR, header=False)
CarDamage: Any = _class_for("CarDamage", CAR_DAMAGE_CAR, header=False)
Participant: Any = _class_for("Participant", PARTICIPANT_CAR, header=False)
CarSetup: Any = _class_for("CarSetup", CAR_SETUP_CAR, header=False)
_HISTORY_LAP_EXTRA = ("sector1_ms", "sector2_ms", "sector3_ms")
LapHistory: Any = _class_for("LapHistory", LAP_HISTORY, _HISTORY_LAP_EXTRA, header=False)
TyreStintHistory: Any = _class_for("TyreStintHistory", TYRE_STINT_HISTORY, header=False)
TyreSetData: Any = _class_for("TyreSetData", TYRE_SET, header=False)
CarTelemetry2: Any = _class_for("CarTelemetry2", CAR_TELEMETRY_2_CAR, header=False)

SessionPacket: Any = _class_for("SessionPacket", SESSION_LAYOUT)
LapDataPacket: Any = _class_for("LapDataPacket", LAP_DATA_LAYOUT)
EventPacket: Any = _class_for("EventPacket", EVENT_LAYOUT, ("code", "detail"))
ParticipantsPacket: Any = _class_for("ParticipantsPacket", PARTICIPANTS_LAYOUT)
CarSetupsPacket: Any = _class_for("CarSetupsPacket", CAR_SETUPS_LAYOUT)
CarTelemetryPacket: Any = _class_for("CarTelemetryPacket", CAR_TELEMETRY_LAYOUT)
CarStatusPacket: Any = _class_for("CarStatusPacket", CAR_STATUS_LAYOUT)
CarDamagePacket: Any = _class_for("CarDamagePacket", CAR_DAMAGE_LAYOUT)
SessionHistoryPacket: Any = _class_for("SessionHistoryPacket", SESSION_HISTORY_LAYOUT)
TyreSetsPacket: Any = _class_for("TyreSetsPacket", TYRE_SETS_LAYOUT)
MotionExPacket: Any = _class_for("MotionExPacket", MOTION_EX_LAYOUT)
CarTelemetry2Packet: Any = _class_for("CarTelemetry2Packet", CAR_TELEMETRY_2_LAYOUT)

_SUB_CLASSES: dict[tuple[Item, ...], Any] = {
    MARSHAL_ZONE: MarshalZone,
    WEATHER_FORECAST_SAMPLE: WeatherForecastSample,
    ACTIVE_AERO_ZONE: Zone,
    LAP_DATA_CAR: LapDataCar,
    CAR_TELEMETRY_CAR: CarTelemetry,
    CAR_STATUS_CAR: CarStatus,
    CAR_DAMAGE_CAR: CarDamage,
    PARTICIPANT_CAR: Participant,
    CAR_SETUP_CAR: CarSetup,
    LAP_HISTORY: LapHistory,
    TYRE_STINT_HISTORY: TyreStintHistory,
    TYRE_SET: TyreSetData,
    CAR_TELEMETRY_2_CAR: CarTelemetry2,
}

_COMPILED: dict[tuple[Item, ...], CompiledLayout] = {}
_PACKET_CLASSES: dict[int, tuple[Any, tuple[Item, ...]]] = {
    PacketId.SESSION: (SessionPacket, SESSION_LAYOUT),
    PacketId.LAP_DATA: (LapDataPacket, LAP_DATA_LAYOUT),
    PacketId.EVENT: (EventPacket, EVENT_LAYOUT),
    PacketId.PARTICIPANTS: (ParticipantsPacket, PARTICIPANTS_LAYOUT),
    PacketId.CAR_SETUPS: (CarSetupsPacket, CAR_SETUPS_LAYOUT),
    PacketId.CAR_TELEMETRY: (CarTelemetryPacket, CAR_TELEMETRY_LAYOUT),
    PacketId.CAR_STATUS: (CarStatusPacket, CAR_STATUS_LAYOUT),
    PacketId.CAR_DAMAGE: (CarDamagePacket, CAR_DAMAGE_LAYOUT),
    PacketId.SESSION_HISTORY: (SessionHistoryPacket, SESSION_HISTORY_LAYOUT),
    PacketId.TYRE_SETS: (TyreSetsPacket, TYRE_SETS_LAYOUT),
    PacketId.MOTION_EX: (MotionExPacket, MOTION_EX_LAYOUT),
    PacketId.CAR_TELEMETRY_2: (CarTelemetry2Packet, CAR_TELEMETRY_2_LAYOUT),
}


def _compiled(layout: tuple[Item, ...]) -> CompiledLayout:
    if layout not in _COMPILED:
        _COMPILED[layout] = compile_layout(layout)
    return _COMPILED[layout]


def _consume(item: Item, values: Iterator[Any]) -> Any:
    if isinstance(item, Field):
        if item.count == 1:
            return next(values)
        v = tuple(next(values) for _ in range(item.count))
        return Corners(*v) if item.corners else v
    cls = _SUB_CLASSES[item.layout]
    return tuple(_fill(cls, item.layout, values) for _ in range(item.n))


def _fill(cls: Any, layout: tuple[Item, ...], values: Iterator[Any]) -> Any:
    kwargs = {item.name: _consume(item, values) for item in layout}
    obj: Any = cls(**kwargs)
    _post_decode(obj)
    return obj


def _post_decode(obj: Any) -> None:
    """Decoded conveniences applied at the parser boundary."""
    if isinstance(obj, LapDataCar):
        # Split ms/minutes pairs decoded to plain milliseconds.
        obj.sector1_ms = obj.sector1_time_minutes_part * 60000 + obj.sector1_time_ms_part
        obj.sector2_ms = obj.sector2_time_minutes_part * 60000 + obj.sector2_time_ms_part
        obj.delta_to_car_in_front_ms = (
            obj.delta_to_car_in_front_minutes_part * 60000 + obj.delta_to_car_in_front_ms_part
        )
        obj.delta_to_race_leader_ms = (
            obj.delta_to_race_leader_minutes_part * 60000 + obj.delta_to_race_leader_ms_part
        )
    elif isinstance(obj, LapHistory):
        obj.sector1_ms = obj.sector1_minutes * 60000 + obj.sector1_ms_part
        obj.sector2_ms = obj.sector2_minutes * 60000 + obj.sector2_ms_part
        obj.sector3_ms = obj.sector3_minutes * 60000 + obj.sector3_ms_part
    elif isinstance(obj, Participant):
        raw_name: bytes = obj.name
        obj.name = raw_name.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    elif isinstance(obj, EventPacket):
        raw: bytes = obj.event_string_code
        obj.code = raw.decode("ascii", errors="replace")
        obj.detail = decode_event_detail(obj.code, obj.event_data)


def parse(packet_id: int, payload: bytes, header: PacketHeader | None = None) -> Any:
    """Parse a datagram (including its 29-byte header) into a packet object."""
    if header is None:
        header = parse_header(payload)
    cls, layout = _PACKET_CLASSES[packet_id]
    compiled = _compiled(layout)
    values = iter(compiled.struct.unpack_from(payload, HEADER_SIZE))
    kwargs = {item.name: _consume(item, values) for item in layout}
    obj: Any = cls(header=header, **kwargs)
    _post_decode(obj)
    return obj


# ---------------------------------------------------------------- Event detail

# Typed views over the 12-byte event detail union, per event code.
_EVENT_DETAIL_STRUCTS: dict[str, tuple[struct.Struct, tuple[str, ...]]] = {
    "FTLP": (struct.Struct("<Bf"), ("vehicle_idx", "lap_time_s")),
    "RTMT": (struct.Struct("<BB"), ("vehicle_idx", "reason")),
    "DRSD": (struct.Struct("<B"), ("reason",)),
    "TMPT": (struct.Struct("<B"), ("vehicle_idx",)),
    "RCWN": (struct.Struct("<B"), ("vehicle_idx",)),
    "PENA": (
        struct.Struct("<BBBBBBB"),
        (
            "penalty_type",
            "infringement_type",
            "vehicle_idx",
            "other_vehicle_idx",
            "time_s",
            "lap_num",
            "places_gained",
        ),
    ),
    "SPTP": (
        struct.Struct("<BfBBBf"),
        (
            "vehicle_idx",
            "speed_kmh",
            "overall_fastest_in_session",
            "driver_fastest_in_session",
            "fastest_vehicle_idx_in_session",
            "fastest_speed_in_session_kmh",
        ),
    ),
    "STLG": (struct.Struct("<B"), ("num_lights",)),
    "DTSV": (struct.Struct("<B"), ("vehicle_idx",)),
    "SGSV": (struct.Struct("<Bf"), ("vehicle_idx", "stop_time_s")),
    "FLBK": (struct.Struct("<If"), ("flashback_frame_identifier", "flashback_session_time")),
    "BUTN": (struct.Struct("<I"), ("button_status",)),
    "OVTK": (struct.Struct("<BB"), ("overtaking_vehicle_idx", "being_overtaken_vehicle_idx")),
    "SCAR": (struct.Struct("<BB"), ("safety_car_type", "event_type")),
    "COLL": (struct.Struct("<BBB"), ("vehicle1_idx", "vehicle2_idx", "severity")),
}


def decode_event_detail(code: str, raw: bytes) -> dict[str, Any] | bytes:
    entry = _EVENT_DETAIL_STRUCTS.get(code)
    if entry is None:
        return raw
    s, names = entry
    return dict(zip(names, s.unpack_from(raw), strict=True))


# ---------------------------------------------------------------- size gate


def _assert_sizes() -> None:
    for packet_id, (_cls, layout) in _PACKET_CLASSES.items():
        body = _compiled(layout).struct.size
        total = HEADER_SIZE + body
        expected = PACKET_SIZES[packet_id]
        assert total == expected, (
            f"packet {PacketId(packet_id).name}: layout totals {total} bytes, "
            f"expected {expected} (off by {total - expected})"
        )


_assert_sizes()

# Byte offsets of named fields inside a packet body (index/seek helpers).
SESSION_TYPE_OFFSET = HEADER_SIZE + _compiled(SESSION_LAYOUT).offsets["session_type"]


def car_field_offset(packet_id: int, car_idx: int, field_name: str) -> int:
    """Absolute byte offset of a per-car field inside a datagram."""
    _cls, layout = _PACKET_CLASSES[packet_id]
    compiled = _compiled(layout)
    cars_item = next(i for i in compiled.layout if i.name == "cars")
    assert isinstance(cars_item, Array)
    sub = _compiled(cars_item.layout)
    return (
        HEADER_SIZE + compiled.offsets["cars"] + car_idx * sub.struct.size + sub.offsets[field_name]
    )
