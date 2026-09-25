from __future__ import annotations

import struct

import pytest

from pitwall.protocol.header import (
    HEADER_SIZE,
    HEADER_STRUCT,
    PACKET_SIZES,
    PacketId,
    is_supported,
    parse_header,
)

from .synth import make_packet


def test_header_struct_size() -> None:
    assert HEADER_STRUCT.size == 29
    assert HEADER_SIZE == 29


def test_header_roundtrip() -> None:
    pkt = make_packet(PacketId.SESSION, session_uid=0x1234, session_time=12.5, frame=42)
    h = parse_header(pkt)
    assert h.packet_format == 2026
    assert h.game_year == 26
    assert h.packet_id == PacketId.SESSION
    assert h.session_uid == 0x1234
    assert h.session_time == pytest.approx(12.5)
    assert h.frame_identifier == 42
    assert h.overall_frame_identifier == 42
    assert h.player_car_index == 0
    assert h.secondary_player_car_index == 255
    assert is_supported(h)


def test_unsupported_format() -> None:
    assert not is_supported(parse_header(make_packet(PacketId.SESSION, packet_format=2025)))


def test_game_year_not_a_gate() -> None:
    assert is_supported(parse_header(make_packet(PacketId.SESSION, game_year=25)))


def test_all_packet_ids_have_sizes() -> None:
    assert set(PACKET_SIZES) == set(PacketId)


def test_short_buffer_raises() -> None:
    with pytest.raises(struct.error):
        parse_header(b"\0" * 10)
