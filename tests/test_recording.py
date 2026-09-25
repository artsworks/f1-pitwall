from __future__ import annotations

import json
from pathlib import Path

from pitwall.net.recording import (
    RecordingReader,
    build_index,
    compress_recording,
    index_path_for,
)
from pitwall.protocol.header import PacketId

from .synth import make_event_packet, make_packet, write_synthetic_recording


def _fixture(path: Path) -> Path:
    packets = [
        make_packet(PacketId.SESSION, frame=1),
        make_event_packet(b"SSTA", frame=2),
        make_packet(PacketId.LAP_DATA, frame=3),
        make_event_packet(b"CHQF", frame=4),
    ]
    return write_synthetic_recording(path, packets)


def test_writer_reader_roundtrip(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    with RecordingReader(path) as reader:
        assert reader.header.packet_format == 2026
        assert reader.header.session_uid == 0xDEADBEEF
        records = list(reader)
    assert len(records) == 4
    offsets = [r[0] for r in records]
    assert offsets == sorted(offsets)
    assert all(len(payload) > 29 for _, payload in records)


def test_zst_roundtrip(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    zst = compress_recording(path)
    assert zst.name.endswith(".f1bin.zst")
    with RecordingReader(path) as plain, RecordingReader(zst) as comp:
        assert list(plain) == list(comp)


def test_index_written_on_close(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    idx = index_path_for(path)
    assert idx.exists()
    entries = json.loads(idx.read_text())
    assert [e["detail"] for e in entries] == ["SSTA", "CHQF"]
    assert all(e["kind"] == "event" for e in entries)


def test_index_rebuildable(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    idx = index_path_for(path)
    on_disk = json.loads(idx.read_text())
    idx.unlink()
    rebuilt = [e.to_dict() for e in build_index(path)]
    assert rebuilt == on_disk
