"""Recording profiles: which datagrams are written to .f1bin, and how often.

Filtering happens on raw datagrams before they reach the writer, so every
profile produces an ordinary .f1bin that replays through the unchanged
ingest path. See docs/adr/0006-recording-profiles.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pitwall.protocol.header import PACKET_ID_OFFSET, PacketId

ProfileName = Literal["full", "lite", "minimal"]
PROFILES: tuple[ProfileName, ...] = ("full", "lite", "minimal")

_ALL = frozenset(int(p) for p in PacketId)
_MOTION = frozenset({PacketId.MOTION, PacketId.MOTION_EX, PacketId.CAR_TELEMETRY_2})


@dataclass(frozen=True, slots=True)
class RecordingProfile:
    """`keep` is the set of packet ids written; `max_hz` caps the rate of a
    packet id (absent = every packet). Event packets are never rate-capped."""

    name: ProfileName
    keep: frozenset[int]
    max_hz: dict[int, float] = field(default_factory=dict)


PROFILE_TABLE: dict[ProfileName, RecordingProfile] = {
    "full": RecordingProfile("full", _ALL),
    "lite": RecordingProfile(
        "lite",
        (_ALL - _MOTION) | {PacketId.MOTION_EX},
        {
            PacketId.MOTION_EX: 10,
            PacketId.LAP_DATA: 10,
            PacketId.CAR_TELEMETRY: 10,
            PacketId.CAR_STATUS: 10,
            PacketId.CAR_DAMAGE: 10,
            PacketId.SESSION_HISTORY: 5,
            PacketId.TYRE_SETS: 2,
            PacketId.LAP_POSITIONS: 1,
        },
    ),
    "minimal": RecordingProfile(
        "minimal",
        frozenset(
            {
                PacketId.SESSION,
                PacketId.LAP_DATA,
                PacketId.EVENT,
                PacketId.PARTICIPANTS,
                PacketId.CAR_TELEMETRY,
                PacketId.CAR_STATUS,
                PacketId.FINAL_CLASSIFICATION,
                PacketId.CAR_DAMAGE,
                PacketId.TYRE_SETS,
                PacketId.MOTION_EX,
            }
        ),
        {
            PacketId.MOTION_EX: 10,
            PacketId.LAP_DATA: 5,
            PacketId.CAR_TELEMETRY: 5,
            PacketId.CAR_STATUS: 5,
            PacketId.CAR_DAMAGE: 5,
            PacketId.TYRE_SETS: 1,
        },
    ),
}


class RecordFilter:
    """Stateful keep/drop decision per datagram for one profile."""

    # Tolerance so a 30 Hz stream capped at 10 Hz keeps every 3rd packet even
    # with receive-time jitter.
    _JITTER_S = 0.005

    def __init__(self, profile: ProfileName | RecordingProfile) -> None:
        self.profile = PROFILE_TABLE[profile] if isinstance(profile, str) else profile
        self._last: dict[int, float] = {}

    def keep(self, t: float, payload: bytes) -> bool:
        """`t` is seconds on any monotonic clock (receive time or record offset)."""
        if len(payload) <= PACKET_ID_OFFSET:
            return True
        pid = payload[PACKET_ID_OFFSET]
        if pid not in self.profile.keep:
            return False
        hz = self.profile.max_hz.get(pid)
        if hz is None:
            return True
        last = self._last.get(pid)
        if last is not None and t - last < 1.0 / hz - self._JITTER_S:
            return False
        self._last[pid] = t
        return True
