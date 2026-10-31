"""Synthetic fixture helpers: fabricate valid format-2026 datagrams.

Bodies are zero-padded to the correct total size; only the header carries real
values. Flagged synthetic per docs/07 — real recordings remain the fixtures.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable, Iterable
from functools import cache
from pathlib import Path

from pitwall.net.recording import RecordingWriter
from pitwall.protocol.header import HEADER_STRUCT, PACKET_SIZES, PacketId
from pitwall.protocol.layouts import Field, Item
from pitwall.protocol.packets import _PACKET_CLASSES, _compiled  # noqa: SLF001


def make_packet(
    packet_id: int,
    *,
    packet_format: int = 2026,
    game_year: int = 26,
    session_uid: int = 0xDEADBEEF,
    session_time: float = 1.0,
    frame: int = 1,
    player: int = 0,
    body: bytes | None = None,
) -> bytes:
    """A datagram with a real header and a body padded to the expected size."""
    header = HEADER_STRUCT.pack(
        packet_format,
        game_year,
        1,
        0,
        1,
        packet_id,
        session_uid,
        session_time,
        frame,
        frame,
        player,
        255,
    )
    expected = PACKET_SIZES[packet_id]
    body_len = expected - HEADER_STRUCT.size
    if body is None:
        body = bytes(body_len)
    return header + body.ljust(body_len, b"\0")[:body_len]


def make_event_packet(code: bytes, **kwargs: float | int) -> bytes:
    """Event packet whose first 4 body bytes are the event code."""
    body = code.ljust(4, b" ")[:4]
    return make_packet(PacketId.EVENT, body=body, **kwargs)  # type: ignore[arg-type]


def write_synthetic_recording(
    path: Path,
    packets: Iterable[bytes],
    *,
    session_uid: int = 0xDEADBEEF,
    spacing_us: int = 33_333,
    metadata: dict[str, object] | None = None,
) -> Path:
    """Write packets to a .f1bin at fixed spacing (≈30 Hz default)."""
    with RecordingWriter(
        path,
        session_uid=session_uid,
        wall_clock_start_us=time.time_ns() // 1000,
        metadata=metadata or {"synthetic": True},
    ) as writer:
        t = 0.0
        for pkt in packets:
            writer.write_datagram(t, pkt)
            t += spacing_us / 1_000_000
    return path


_Flattener = Callable[[dict[str, object], list[object]], None]


def _field_flattener(item: Field) -> _Flattener:
    name = item.name
    if item.count == 1:
        if item.fmt.endswith("s"):
            size = struct.calcsize("<" + item.fmt)
            zero = b"\0" * size

            def flatten(data: dict[str, object], values: list[object]) -> None:
                value = data.get(name)
                if value is None:
                    value = zero
                elif isinstance(value, str):
                    value = value.encode("utf-8")
                if not isinstance(value, bytes):
                    value = bytes(value)  # type: ignore[arg-type]
                values.append(value)

            return flatten

        def flatten(data: dict[str, object], values: list[object]) -> None:
            value = data.get(name)
            if value is None:
                value = 0
            elif isinstance(value, str):
                value = value.encode("utf-8")
            values.append(value)

        return flatten

    count = item.count
    zeros = (0,) * count

    def flatten(data: dict[str, object], values: list[object]) -> None:
        value = data.get(name)
        if value is None:
            values.extend(zeros)
            return
        sequence = list(value)  # type: ignore[arg-type]
        values.extend(sequence[:count])
        if len(sequence) < count:
            values.extend(zeros[: count - len(sequence)])

    return flatten


def _layout_flatteners(layout: tuple[Item, ...]) -> tuple[_Flattener, ...]:
    flatteners: list[_Flattener] = []
    for item in layout:
        if isinstance(item, Field):
            flatteners.append(_field_flattener(item))
            continue
        child_flatteners = _layout_flatteners(item.layout)

        def flatten_array(
            data: dict[str, object],
            values: list[object],
            *,
            name: str = item.name,
            count: int = item.n,
            children: tuple[_Flattener, ...] = child_flatteners,
        ) -> None:
            array = data.get(name, {})
            if not isinstance(array, dict):
                array = {}
            for index in range(count):
                child = array.get(index, {})
                for flattener in children:
                    flattener(child, values)

        flatteners.append(flatten_array)
    return tuple(flatteners)


@cache
def _packet_plan(packet_id: int) -> tuple[struct.Struct, tuple[_Flattener, ...]]:
    _cls, layout = _PACKET_CLASSES[packet_id]
    return _compiled(layout).struct, _layout_flatteners(layout)


def pack_packet(
    packet_id: int,
    data: dict[str, object] | None = None,
    *,
    session_uid: int = 0xDEADBEEF,
    session_time: float = 1.0,
    frame: int = 1,
    player: int = 0,
) -> bytes:
    """Pack a real packet from a layout table. `data` maps field names to
    values; `data['cars']` maps car index -> per-car field dict."""
    packer, flatteners = _packet_plan(packet_id)
    values: list[object] = []
    packet_data = data or {}
    for flattener in flatteners:
        flattener(packet_data, values)
    body = packer.pack(*values)
    header = HEADER_STRUCT.pack(
        2026, 26, 1, 0, 1, packet_id, session_uid, session_time, frame, frame, player, 255
    )
    pkt = header + body
    assert len(pkt) == PACKET_SIZES[packet_id]
    return pkt


def out_lap_scenario(
    *,
    cold_s: float = 5.0,
    warm_temp: int = 95,
    cold_temp: int = 60,
    rate_hz: float = 30.0,
    laps: int = 2,
    compound: int | None = None,
) -> list[tuple[float, bytes]]:
    """(t, packet) stream: race session, player on an out-lap in sector 3, FL
    inner temp cold for `cold_s` then warm (other tyres warm). Advances
    session_time like the real game."""
    packets: list[tuple[float, bytes]] = []
    dt = 1.0 / rate_hz
    total_s = cold_s + 5.0
    n = int(total_s * rate_hz)
    for i in range(n + int(laps * rate_hz)):
        t = i * dt
        temp = cold_temp if t < cold_s else warm_temp
        lap = 1 + int(t // (total_s))
        packets.append(
            (
                t,
                pack_packet(
                    PacketId.CAR_TELEMETRY,
                    {
                        "cars": {
                            0: {
                                "tyres_inner_temperature": (warm_temp, warm_temp, temp, warm_temp),
                                "tyres_surface_temperature": (
                                    warm_temp,
                                    warm_temp,
                                    temp,
                                    warm_temp,
                                ),
                            }
                        }
                    },
                    session_time=t,
                ),
            )
        )
        packets.append(
            (
                t,
                pack_packet(
                    PacketId.LAP_DATA,
                    {
                        "cars": {
                            0: {
                                "driver_status": 3,  # out lap
                                "pit_status": 0,
                                "current_lap_num": lap,
                                "sector": 2,
                                "lap_distance": 100.0 + i,
                            }
                        }
                    },
                    session_time=t,
                ),
            )
        )
        if compound is not None:
            packets.append(
                (
                    t,
                    pack_packet(
                        PacketId.CAR_STATUS,
                        {"cars": {0: {"actual_tyre_compound": compound}}},
                        session_time=t,
                    ),
                )
            )
        if i % int(rate_hz / 2) == 0:
            packets.append(
                (
                    t,
                    pack_packet(
                        PacketId.SESSION,
                        {
                            "session_type": 15,
                            "track_id": 7,
                            "total_laps": 50,
                        },
                        session_time=t,
                    ),
                )
            )
    return packets


def write_packet_stream(
    path: Path,
    packets: list[tuple[float, bytes]],
    *,
    session_uid: int = 0xDEADBEEF,
    metadata: dict[str, object] | None = None,
    wall_clock_start_us: int | None = None,
) -> Path:
    meta: dict[str, object] = {"synthetic": True, **(metadata or {})}
    with RecordingWriter(
        path, session_uid=session_uid, metadata=meta, wall_clock_start_us=wall_clock_start_us
    ) as writer:
        for t, pkt in packets:
            writer.write_datagram(t, pkt)
    return path


def track_limits_scenario(
    *,
    events: bool = True,
    lap_counts: bool = True,
    infringements: tuple[int, ...] = (27, 28, 29),
    vehicle_idx: int = 0,
    session_type: int = 15,
    penalty: bool = True,
) -> list[tuple[float, bytes]]:
    """Synthetic Austria warnings on laps 2, 3, 4, then a penalty on lap 5."""
    packets: list[tuple[float, bytes]] = []
    for frame in range(501):
        t = frame / 10
        lap = 1 + int(t // 10)
        count = min(lap - 1, 4) if lap_counts else 0

        def emit(
            packet_id: int, data: dict[str, object], *, t: float = t, frame: int = frame
        ) -> None:
            packets.append((t, pack_packet(packet_id, data, session_time=t, frame=frame)))

        emit(
            PacketId.SESSION,
            {"session_type": session_type, "track_id": 17, "track_length": 4318, "total_laps": 71},
        )
        emit(
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: {
                        "current_lap_num": lap,
                        "car_position": 3,
                        "result_status": 2,
                        "driver_status": 4,
                        "lap_distance": (t % 10) * 431.8,
                        "corner_cutting_warnings": count,
                        "total_warnings": count,
                        "penalties": 5 if penalty and lap >= 5 else 0,
                    }
                }
            },
        )
        emit(PacketId.CAR_TELEMETRY, {"cars": {0: {"speed": 220, "throttle": 1.0}}})
        if events and frame in (100, 200, 300) and lap - 2 < len(infringements):
            detail = struct.pack("<BBBBBBB", 5, infringements[lap - 2], vehicle_idx, 255, 0, lap, 0)
            emit(
                PacketId.EVENT,
                {"event_string_code": b"PENA", "event_data": detail.ljust(12, b"\0")},
            )
        if penalty and frame == 400:
            detail = struct.pack("<BBBBBBB", 4, 21, 0, 255, 5, lap, 0)
            emit(
                PacketId.EVENT,
                {"event_string_code": b"PENA", "event_data": detail.ljust(12, b"\0")},
            )
    return packets


def mixed_session_packets(n_frames: int = 10, session_uid: int = 0xDEADBEEF) -> list[bytes]:
    """A plausible mix: menu-rate packets + session + an event, n_frames times."""
    menu_ids = [
        PacketId.MOTION,
        PacketId.LAP_DATA,
        PacketId.CAR_TELEMETRY,
        PacketId.CAR_STATUS,
    ]
    out: list[bytes] = []
    for i in range(n_frames):
        frame = i + 1
        for pid in menu_ids:
            out.append(make_packet(pid, session_uid=session_uid, frame=frame))
        if i % 5 == 0:
            out.append(make_packet(PacketId.SESSION, session_uid=session_uid, frame=frame))
        if i == 3:
            out.append(make_event_packet(b"SSTA", session_uid=session_uid, frame=frame))
    return out
