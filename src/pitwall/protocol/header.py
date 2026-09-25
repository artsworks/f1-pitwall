"""Format-2026 packet header parsing.

Layout is re-expressed as data from docs/reference/f1-26-udp-notes.md; the game
emits other formats too, but we accept F1 26 / format 2026 only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

PACKET_FORMAT = 2026
GAME_YEAR = 26

# uint16 m_packetFormat
# uint8  m_gameYear, m_gameMajorVersion, m_gameMinorVersion, m_packetVersion, m_packetId
# uint64 m_sessionUID
# float  m_sessionTime
# uint32 m_frameIdentifier        (rewinds on flashback)
# uint32 m_overallFrameIdentifier (monotonic across flashbacks)
# uint8  m_playerCarIndex, m_secondaryPlayerCarIndex
HEADER_STRUCT = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER_STRUCT.size
assert HEADER_SIZE == 29, HEADER_SIZE

# Offset of m_packetId inside the header (used for cheap pre-parse peeking).
PACKET_ID_OFFSET = 6
# Offset of m_sessionUID inside the header.
SESSION_UID_OFFSET = 7
# Offset of m_playerCarIndex inside the header.
PLAYER_CAR_INDEX_OFFSET = 27


class PacketId(IntEnum):
    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    FINAL_CLASSIFICATION = 8
    LOBBY_INFO = 9
    CAR_DAMAGE = 10
    SESSION_HISTORY = 11
    TYRE_SETS = 12
    MOTION_EX = 13
    TIME_TRIAL = 14
    LAP_POSITIONS = 15
    CAR_TELEMETRY_2 = 16


# Expected total datagram sizes (header + body), format 2026.
PACKET_SIZES: dict[int, int] = {
    PacketId.MOTION: 1325,
    PacketId.SESSION: 926,
    PacketId.LAP_DATA: 1399,
    PacketId.EVENT: 45,
    PacketId.PARTICIPANTS: 1470,
    PacketId.CAR_SETUPS: 1233,
    PacketId.CAR_TELEMETRY: 1448,
    PacketId.CAR_STATUS: 1445,
    PacketId.FINAL_CLASSIFICATION: 1134,
    PacketId.LOBBY_INFO: 1062,
    PacketId.CAR_DAMAGE: 1133,
    PacketId.SESSION_HISTORY: 1460,
    PacketId.TYRE_SETS: 231,
    PacketId.MOTION_EX: 273,
    PacketId.TIME_TRIAL: 104,
    PacketId.LAP_POSITIONS: 1231,
    PacketId.CAR_TELEMETRY_2: 269,
}

# Byte offset of the 4-char event code inside an Event packet (right after the header).
EVENT_CODE_OFFSET = HEADER_SIZE
EVENT_CODE_LEN = 4


@dataclass(slots=True, frozen=True)
class PacketHeader:
    packet_format: int
    game_year: int
    game_major_version: int
    game_minor_version: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int
    secondary_player_car_index: int


def parse_header(buf: bytes) -> PacketHeader:
    """Unpack the 29-byte header. Raises struct.error if buf is too short."""
    fields = HEADER_STRUCT.unpack_from(buf)
    return PacketHeader(*fields)


def is_supported(header: PacketHeader) -> bool:
    return header.packet_format == PACKET_FORMAT and header.game_year == GAME_YEAR
