from __future__ import annotations

from collections import Counter
from pathlib import Path

from pitwall.cli import main
from pitwall.net.profile import PROFILE_TABLE, RecordFilter
from pitwall.net.recording import RecordingReader, RecordingRotator
from pitwall.protocol.header import PacketId

from .synth import make_event_packet, make_packet


def _stream(seconds: float = 2.0, hz: int = 30) -> list[tuple[float, bytes]]:
    out: list[tuple[float, bytes]] = []
    for i in range(int(seconds * hz)):
        t = i / hz
        for pid in (PacketId.MOTION, PacketId.LAP_DATA, PacketId.CAR_TELEMETRY):
            out.append((t, make_packet(pid, frame=i)))
        if i % 15 == 0:
            out.append((t, make_event_packet(b"BUTN", frame=i)))
    return out


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


def test_start_parser_accepts_record_flag() -> None:
    from pitwall.cli import build_parser

    args = build_parser().parse_args(["start", "--record", "full"])
    assert args.record == "full"
    assert build_parser().parse_args(["start"]).record is None
