"""Synthetic fixture helpers: fabricate valid format-2026 datagrams.

Bodies are zero-padded to the correct total size; only the header carries real
values. Flagged synthetic per docs/07 — real recordings remain the fixtures.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from pitwall.net.recording import RecordingWriter
from pitwall.protocol.header import HEADER_STRUCT, PACKET_SIZES, PacketId
from pitwall.protocol.pack import (
    _field_values as _field_values,
)
from pitwall.protocol.pack import (
    _layout_values as _layout_values,
)
from pitwall.protocol.pack import (
    pack_packet,
)


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
) -> Path:
    meta: dict[str, object] = {"synthetic": True, **(metadata or {})}
    with RecordingWriter(path, session_uid=session_uid, metadata=meta) as writer:
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
