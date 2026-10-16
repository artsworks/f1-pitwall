from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from pitwall.cli import main
from pitwall.net.profile import PROFILE_TABLE, ProfileName, RecordFilter
from pitwall.net.recording import (
    RecordingReader,
    RecordingRotator,
    RecordingWriter,
    compress_recording,
    index_path_for,
)
from pitwall.protocol.header import PacketId

from .synth import make_event_packet, make_packet

_TRIM_WALL_CLOCK_US = 1_700_000_000_000_000


def _stream(seconds: float = 2.0, hz: int = 30) -> list[tuple[float, bytes]]:
    out: list[tuple[float, bytes]] = []
    for i in range(int(seconds * hz)):
        t = i / hz
        for pid in (PacketId.MOTION, PacketId.LAP_DATA, PacketId.CAR_TELEMETRY):
            out.append((t, make_packet(pid, frame=i)))
        if i % 15 == 0:
            out.append((t, make_event_packet(b"BUTN", frame=i)))
    return out


def _write_trim_recording(
    path: Path,
    *,
    profile: ProfileName | None = None,
    wall_clock_start_us: int = _TRIM_WALL_CLOCK_US,
) -> Path:
    metadata = {"profile": profile} if profile is not None else {}
    record_filter = RecordFilter(profile) if profile is not None else None
    with RecordingWriter(
        path,
        session_uid=0x1234,
        wall_clock_start_us=wall_clock_start_us,
        metadata=metadata,
    ) as writer:
        for timestamp, payload in _stream():
            if record_filter is None or record_filter.keep(timestamp, payload):
                writer.write_datagram(timestamp, payload)
    return path


def _kept(profile: str) -> Counter[int]:
    f = RecordFilter(profile)  # type: ignore[arg-type]
    return Counter(p[6] for t, p in _stream() if f.keep(t, p))


def test_full_keeps_everything() -> None:
    kept = _kept("full")
    assert kept[PacketId.MOTION] == 60
    assert kept[PacketId.CAR_TELEMETRY] == 60


def test_lite_drops_motion_and_caps_rate() -> None:
    kept = _kept("lite")
    assert kept[PacketId.MOTION] == 0
    assert kept[PacketId.CAR_TELEMETRY] == 20  # 10 Hz over 2 s
    assert kept[PacketId.LAP_DATA] == 20
    assert kept[PacketId.EVENT] == 4


def test_minimal_is_5hz() -> None:
    kept = _kept("minimal")
    assert kept[PacketId.MOTION] == 0
    assert kept[PacketId.CAR_TELEMETRY] == 10
    assert kept[PacketId.EVENT] == 4


def test_every_rule_input_survives_lite_and_minimal() -> None:
    needed = {
        PacketId.SESSION,
        PacketId.LAP_DATA,
        PacketId.EVENT,
        PacketId.CAR_TELEMETRY,
        PacketId.CAR_STATUS,
        PacketId.CAR_DAMAGE,
    }
    for name in ("lite", "minimal"):
        assert needed <= PROFILE_TABLE[name].keep


def test_rotator_filters_compresses_and_stamps_profile(tmp_path: Path) -> None:
    rot = RecordingRotator(tmp_path, profile="lite", compress=True)
    for t, p in _stream():
        rot.write_datagram(t, p)
    rot.close()
    assert rot.last_path is not None and rot.last_path.name.endswith(".f1bin.zst")
    assert not list(tmp_path.glob("*.f1bin"))
    with RecordingReader(rot.last_path) as r:
        assert r.header.metadata["profile"] == "lite"
        pids = Counter(p[6] for _, p in r)
    assert pids[PacketId.MOTION] == 0
    assert pids[PacketId.CAR_TELEMETRY] == 20


def test_trim_profile_downsamples_full_recording(tmp_path: Path) -> None:
    rot = RecordingRotator(tmp_path, profile="full")
    for t, p in _stream():
        rot.write_datagram(t, p)
    rot.close()
    assert rot.last_path is not None
    out = tmp_path / "lite.f1bin"
    assert main(["trim", str(rot.last_path), "--profile", "lite", "--out", str(out)]) == 0
    with RecordingReader(out) as r:
        assert r.header.metadata["profile"] == "lite"
        pids = Counter(p[6] for _, p in r)
    assert pids[PacketId.MOTION] == 0
    assert pids[PacketId.CAR_TELEMETRY] == 20


def test_trim_rejects_source_as_output_without_writing(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "source.f1bin")
    original = source.read_bytes()

    assert main(["trim", str(source), "--out", str(source)]) == 2
    assert source.read_bytes() == original

    compressed_alias = source.with_name(source.name + ".zst")
    assert main(["trim", str(source), "--out", str(compressed_alias)]) == 2
    assert source.read_bytes() == original
    assert not compressed_alias.exists()


def test_trim_rejects_normalized_output_alias(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    source = _write_trim_recording(directory / "source.f1bin")
    alias = directory / ".." / "dir" / source.name
    original = source.read_bytes()

    assert main(["trim", str(source), "--out", str(alias)]) == 2
    assert source.read_bytes() == original


def test_trim_rejects_symlink_output_alias(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "source.f1bin")
    alias = tmp_path / "alias.f1bin"
    try:
        alias.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    original = source.read_bytes()

    assert main(["trim", str(source), "--out", str(alias)]) == 2
    assert source.read_bytes() == original


def test_trim_rejects_hard_link_output_alias(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "source.f1bin")
    alias = tmp_path / "hard-link.f1bin"
    try:
        os.link(source, alias)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    original = source.read_bytes()

    assert main(["trim", str(source), "--out", str(alias)]) == 2
    assert source.read_bytes() == original
    assert alias.read_bytes() == original


def test_trim_rejects_shared_source_index_path(tmp_path: Path) -> None:
    raw_source = _write_trim_recording(tmp_path / "race.f1bin")
    source = compress_recording(raw_source, remove=True)
    index = index_path_for(source)
    original_index = index.read_bytes()
    output = tmp_path / "race.f1bin"

    assert main(["trim", str(source), "--out", str(output)]) == 2
    assert index.read_bytes() == original_index
    assert not output.exists()


def test_trim_preserves_wall_clock_and_rebases_to_first_kept_record(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "source.f1bin")
    full_copy = tmp_path / "full-copy.f1bin"
    assert main(["trim", str(source), "--out", str(full_copy)]) == 0
    with RecordingReader(full_copy) as reader:
        assert reader.header.wall_clock_start_us == _TRIM_WALL_CLOCK_US

    range_copy = tmp_path / "range-copy.f1bin"
    from_us = 40_000
    with RecordingReader(source) as reader:
        first_kept_offset_us = next(offset for offset, _ in reader if offset >= from_us)
    assert main(["trim", str(source), "--from-us", str(from_us), "--out", str(range_copy)]) == 0
    with RecordingReader(range_copy) as reader:
        assert reader.header.wall_clock_start_us == _TRIM_WALL_CLOCK_US + first_kept_offset_us
        assert next(iter(reader))[0] == 0

    empty_copy = tmp_path / "empty-copy.f1bin"
    empty_from_us = 10_000_000
    assert (
        main(["trim", str(source), "--from-us", str(empty_from_us), "--out", str(empty_copy)]) == 0
    )
    with RecordingReader(empty_copy) as reader:
        assert reader.header.wall_clock_start_us == _TRIM_WALL_CLOCK_US + empty_from_us
        assert list(reader) == []


def test_trim_compresses_zst_output(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "source.f1bin")
    output = tmp_path / "copy.f1bin.zst"

    assert main(["trim", str(source), "--out", str(output)]) == 0
    assert output.exists()
    assert not output.with_suffix("").exists()
    with RecordingReader(output) as reader:
        assert sum(1 for _ in reader) == len(_stream())


def test_trim_preserves_source_profile_and_rejects_upgrades(tmp_path: Path) -> None:
    lite_source = _write_trim_recording(tmp_path / "lite.f1bin", profile="lite")
    minimal_source = _write_trim_recording(tmp_path / "minimal.f1bin", profile="minimal")
    preserved = tmp_path / "preserved.f1bin"
    assert main(["trim", str(lite_source), "--out", str(preserved)]) == 0
    with RecordingReader(preserved) as reader:
        assert reader.header.metadata["profile"] == "lite"

    assert (
        main(
            [
                "trim",
                str(lite_source),
                "--profile",
                "full",
                "--out",
                str(tmp_path / "lite-to-full.f1bin"),
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "trim",
                str(minimal_source),
                "--profile",
                "lite",
                "--out",
                str(tmp_path / "minimal-to-lite.f1bin"),
            ]
        )
        == 2
    )


def test_trim_rejects_non_recording_output_name(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "race.f1bin")
    index = index_path_for(source)
    original_index = index.read_bytes()

    assert main(["trim", str(source), "--out", str(tmp_path / "race.f1idx")]) == 2
    assert index.read_bytes() == original_index
    assert main(["trim", str(source), "--out", str(tmp_path / "copy.zip")]) == 2


def test_trim_zst_output_does_not_remove_existing_raw(tmp_path: Path) -> None:
    source = _write_trim_recording(tmp_path / "a.f1bin")
    other = _write_trim_recording(tmp_path / "b.f1bin")
    original = other.read_bytes()

    assert main(["trim", str(source), "--out", str(tmp_path / "b.f1bin.zst")]) == 2
    assert other.read_bytes() == original
    assert not tmp_path.joinpath("b.f1bin.zst").exists()


def test_start_parser_accepts_record_flag() -> None:
    from pitwall.cli import build_parser

    args = build_parser().parse_args(["start", "--record", "full"])
    assert args.record == "full"
    assert args.log_level == "warning"
    default_args = build_parser().parse_args(["start"])
    assert default_args.record is None
    assert default_args.log_level == "warning"
    debug_args = build_parser().parse_args(["--log-level", "debug", "start"])
    assert debug_args.log_level == "debug"
