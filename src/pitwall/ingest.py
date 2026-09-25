"""Ingest: parse headers, count, and dispatch datagrams to handlers.

Every datagram is optionally written to a RecordingWriter BEFORE parsing, so a
recording stays valid even when the parser is wrong.
"""

from __future__ import annotations

import struct
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, Protocol

from pitwall.protocol.header import (
    HEADER_SIZE,
    PACKET_SIZES,
    PacketHeader,
    is_supported,
    parse_header,
)

RATE_WINDOW_S = 5.0

PacketHandler = Callable[[PacketHeader, bytes], None]


class DatagramRecorder(Protocol):
    def write_datagram(self, recv_time: float, payload: bytes) -> None: ...


class Ingest:
    def __init__(self, recorder: DatagramRecorder | None = None) -> None:
        self._recorder = recorder
        self._handlers: dict[int, list[PacketHandler]] = defaultdict(list)
        self._accepted: dict[int, int] = defaultdict(int)
        self._dropped_size: dict[int, int] = defaultdict(int)
        self._dropped_unsupported = 0
        self._dropped_malformed = 0
        self._arrivals: dict[int, deque[float]] = defaultdict(deque)
        self.last_session_uid: int = 0

    def register(self, packet_id: int, handler: PacketHandler) -> None:
        self._handlers[packet_id].append(handler)

    def on_datagram(self, payload: bytes, recv_time: float) -> None:
        if self._recorder is not None:
            self._recorder.write_datagram(recv_time, payload)
        if len(payload) < HEADER_SIZE:
            self._dropped_malformed += 1
            return
        try:
            header = parse_header(payload)
        except struct.error:
            self._dropped_malformed += 1
            return
        if not is_supported(header):
            self._dropped_unsupported += 1
            return
        self.last_session_uid = header.session_uid
        expected = PACKET_SIZES.get(header.packet_id)
        if expected is None or len(payload) != expected:
            self._dropped_size[header.packet_id] += 1
            return
        self._accepted[header.packet_id] += 1
        arrivals = self._arrivals[header.packet_id]
        arrivals.append(recv_time)
        while arrivals and recv_time - arrivals[0] > RATE_WINDOW_S:
            arrivals.popleft()
        for handler in self._handlers[header.packet_id]:
            handler(header, payload)

    def rate_hz(self, packet_id: int, now: float) -> float:
        """Observed rate over the last RATE_WINDOW_S seconds."""
        arrivals = self._arrivals.get(packet_id)
        if not arrivals:
            return 0.0
        while arrivals and now - arrivals[0] > RATE_WINDOW_S:
            arrivals.popleft()
        return len(arrivals) / RATE_WINDOW_S

    def census(self, now: float | None = None) -> dict[str, Any]:
        """Stats dict for --stats / doctor."""
        packets: dict[str, Any] = {}
        for pid in sorted(set(self._accepted) | set(self._dropped_size) | set(self._arrivals)):
            entry: dict[str, Any] = {
                "accepted": self._accepted.get(pid, 0),
                "dropped_size_mismatch": self._dropped_size.get(pid, 0),
            }
            if now is not None:
                entry["rate_hz"] = round(self.rate_hz(pid, now), 2)
            packets[str(pid)] = entry
        return {
            "packets": packets,
            "dropped_unsupported": self._dropped_unsupported,
            "dropped_malformed": self._dropped_malformed,
            "session_uid": self.last_session_uid,
        }
