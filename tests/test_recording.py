from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest
import zstandard

from pitwall.net import recording
from pitwall.net.recording import (
    RecordingReader,
    RecordingRotator,
    RecordingWriter,
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


def test_compress_retries_locked_source_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    original = path.read_bytes()
    original_unlink = Path.unlink
    attempts = 0

    def flaky_unlink(self: Path, *, missing_ok: bool = False) -> None:
        nonlocal attempts
        if self == path:
            attempts += 1
            if attempts <= 2:
                raise PermissionError("file in use")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(recording, "_UNLINK_RETRY_S", (0.0, 0.0, 0.0))

    dst = compress_recording(path, remove=True)

    assert attempts == 3
    assert not path.exists()
    with dst.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as reader:
            assert reader.read() == original


def test_compress_keeps_locked_source_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    original_unlink = Path.unlink

    def locked_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self == path:
            raise PermissionError("file in use")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    monkeypatch.setattr(recording, "_UNLINK_RETRY_S", (0.0, 0.0))

    with caplog.at_level(logging.WARNING, logger=recording.__name__):
        dst = compress_recording(path, remove=True)

    assert path.exists()
    assert dst.exists()
    assert "file in use after compressing" in caplog.text
    assert "`pitwall cleanup` removes it" in caplog.text


def test_rotator_keeps_locked_sources_without_thread_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_unlink = Path.unlink
    errors: list[BaseException] = []

    def locked_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self.suffix == ".f1bin":
            raise PermissionError("file in use")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    monkeypatch.setattr(recording, "_UNLINK_RETRY_S", (0.0, 0.0))
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    rotator = RecordingRotator(tmp_path, profile="full", compress=True)
    rotator.write_datagram(0.0, make_event_packet(b"SSTA", session_uid=0xA1))
    rotator.write_datagram(1.0, make_event_packet(b"SSTA", session_uid=0xB2))
    sources = list(tmp_path.glob("session_*.f1bin"))

    rotator.close()

    assert len(sources) == 2
    assert errors == []
    assert all(path.exists() for path in sources)
    assert all(path.with_name(path.name + ".zst").is_file() for path in sources)


def test_rotator_keeps_source_when_compression_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors: list[BaseException] = []

    def fail_compression(path: Path, **kwargs: object) -> Path:
        path.with_name(path.name + ".zst").write_bytes(b"partial")
        raise OSError("compression failed")

    monkeypatch.setattr(recording, "compress_recording", fail_compression)
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    rotator = RecordingRotator(tmp_path, profile="full", compress=True)
    rotator.write_datagram(0.0, make_event_packet(b"SSTA", session_uid=0xA1))
    rotator.write_datagram(1.0, make_event_packet(b"SSTA", session_uid=0xB2))
    sources = list(tmp_path.glob("session_*.f1bin"))

    rotator.close()

    assert len(sources) == 2
    assert errors == []
    assert all(path.exists() for path in sources)
    assert list(tmp_path.glob("*.f1bin.zst")) == []
    assert rotator.last_path in sources


def test_index_written_on_close(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    idx = index_path_for(path)
    assert idx.exists()
    entries = json.loads(idx.read_text())
    events = [e["detail"] for e in entries if e["kind"] == "event"]
    assert events == ["SSTA", "CHQF"]
    assert any(e["kind"] == "session_type" for e in entries)


def test_index_rebuildable(tmp_path: Path) -> None:
    path = _fixture(tmp_path / "s.f1bin")
    idx = index_path_for(path)
    on_disk = json.loads(idx.read_text())
    idx.unlink()
    rebuilt = [e.to_dict() for e in build_index(path)]
    assert rebuilt == on_disk


def test_long_session_offsets_do_not_wrap(tmp_path: Path) -> None:
    """uint32 absolute offsets wrap at ~71.6 min; delta encoding must not."""
    path = tmp_path / "long.f1bin"
    packets = [make_packet(PacketId.SESSION, frame=1), make_packet(PacketId.LAP_DATA, frame=2)]
    with RecordingWriter(path) as writer:
        writer.write_datagram(0.0, packets[0])
        writer.write_datagram(5000.0, packets[1])  # ~83 min later
    with RecordingReader(path) as reader:
        records = list(reader)
    assert len(records) == 2
    assert records[0][0] == 0
    assert records[1][0] == 5_000_000_000
    assert records[0][1] == packets[0]
    assert records[1][1] == packets[1]
