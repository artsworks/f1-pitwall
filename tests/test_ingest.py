from __future__ import annotations

from pitwall.ingest import Ingest
from pitwall.protocol.header import PACKET_SIZES, PacketId, parse_header

from .synth import make_packet


def test_accepted_and_handlers() -> None:
    ingest = Ingest()
    seen: list[int] = []
    ingest.register(PacketId.SESSION, lambda h, p, t: seen.append(h.packet_id))
    for i in range(3):
        ingest.on_datagram(make_packet(PacketId.SESSION, frame=i), recv_time=float(i))
    census = ingest.census()
    assert census["packets"][str(PacketId.SESSION)]["accepted"] == 3
    assert seen == [PacketId.SESSION] * 3


def test_drops_unsupported_format() -> None:
    ingest = Ingest()
    ingest.on_datagram(make_packet(PacketId.SESSION, packet_format=2025), recv_time=0.0)
    ingest.on_datagram(make_packet(PacketId.SESSION, game_year=25), recv_time=0.0)
    census = ingest.census()
    assert census["dropped_unsupported"] == 2
    assert census["packets"] == {}


def test_drops_size_mismatch() -> None:
    ingest = Ingest()
    good = make_packet(PacketId.SESSION)
    ingest.on_datagram(good[:-4], recv_time=0.0)  # truncated body
    ingest.on_datagram(good + b"\x00", recv_time=0.0)  # padded body
    census = ingest.census()
    assert census["packets"][str(PacketId.SESSION)]["dropped_size_mismatch"] == 2
    assert census["packets"][str(PacketId.SESSION)]["accepted"] == 0


def test_drops_malformed() -> None:
    ingest = Ingest()
    ingest.on_datagram(b"\x01\x02\x03", recv_time=0.0)
    assert ingest.census()["dropped_malformed"] == 1


def test_sizes_cover_all_ids() -> None:
    ingest = Ingest()
    for pid, size in PACKET_SIZES.items():
        pkt = make_packet(pid)
        assert len(pkt) == size
        ingest.on_datagram(pkt, recv_time=0.0)
        h = parse_header(pkt)
        assert h.packet_id == pid
    assert sum(e["accepted"] for e in ingest.census()["packets"].values()) == len(PACKET_SIZES)


def test_rate_window() -> None:
    ingest = Ingest()
    for i in range(10):
        ingest.on_datagram(make_packet(PacketId.LAP_DATA, frame=i), recv_time=4.0 + i * 0.1)
    assert ingest.rate_hz(PacketId.LAP_DATA, now=4.9) == 10 / 5.0
    assert ingest.rate_hz(PacketId.LAP_DATA, now=100.0) == 0.0
