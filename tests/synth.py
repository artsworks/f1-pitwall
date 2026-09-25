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
