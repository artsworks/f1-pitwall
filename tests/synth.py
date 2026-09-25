"""Synthetic fixture helpers: fabricate valid format-2026 datagrams.

Bodies are zero-padded to the correct total size; only the header carries real
values. Flagged synthetic per docs/07 — real recordings remain the fixtures.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Iterable
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


def _field_values(item: Field, value: object) -> list[object]:
    if item.count == 1:
        if value is None:
            return [b"\0" * struct.calcsize("<" + item.fmt) if item.fmt.endswith("s") else 0]
        if item.fmt.endswith("s") and not isinstance(value, bytes):
            return [bytes(value)]  # type: ignore[arg-type]
        return [value]
    n = item.count
    if value is None:
        return [0] * n
    seq = list(value)  # type: ignore[arg-type]
    return (seq + [0] * n)[:n]


def _layout_values(layout: tuple[Item, ...], data: dict[str, object]) -> list[object]:
    vals: list[object] = []
    for item in layout:
        if isinstance(item, Field):
            vals.extend(_field_values(item, data.get(item.name)))
        else:
            cars = data.get(item.name, {})
            for i in range(item.n):
                sub = cars.get(i, {}) if isinstance(cars, dict) else {}
                vals.extend(_layout_values(item.layout, sub))
    return vals


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
    _cls, layout = _PACKET_CLASSES[packet_id]
    compiled = _compiled(layout)
    body = compiled.struct.pack(*_layout_values(layout, data or {}))
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
) -> list[tuple[float, bytes]]:
    """(t, packet) stream: race session, player on an out-lap, FL inner temp
    cold for `cold_s` then warm. Advances session_time like the real game."""
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
                                "tyres_inner_temperature": (0, 0, temp, 0),
                                "tyres_surface_temperature": (0, 0, temp, 0),
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
                                "lap_distance": 100.0 + i,
                            }
                        }
                    },
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
    path: Path, packets: list[tuple[float, bytes]], *, session_uid: int = 0xDEADBEEF
) -> Path:
    with RecordingWriter(path, session_uid=session_uid, metadata={"synthetic": True}) as writer:
        for t, pkt in packets:
            writer.write_datagram(t, pkt)
    return path


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
