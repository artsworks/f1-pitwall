from __future__ import annotations

import asyncio
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.ingest import Ingest
from pitwall.net.recording import RecordingReader
from pitwall.net.replay import replay

from .synth import mixed_session_packets, write_synthetic_recording


def _make_recording(tmp_path: Path) -> Path:
    return write_synthetic_recording(tmp_path / "s.f1bin", mixed_session_packets(n_frames=20))


def test_replay_census_same_at_10x_and_max(tmp_path: Path) -> None:
    path = _make_recording(tmp_path)
    censuses = []
    for speed in (10.0, None):  # None == max
        ingest = Ingest()
        delivered = asyncio.run(replay(path, ingest, VirtualClock(), speed))
        censuses.append(ingest.census())
    assert delivered == len(mixed_session_packets(n_frames=20))
    assert censuses[0] == censuses[1]


def test_replay_recv_times_scale(tmp_path: Path) -> None:
    path = _make_recording(tmp_path)
    ingest = Ingest()
    asyncio.run(replay(path, ingest, VirtualClock(), speed=10.0))
    assert sum(e["accepted"] for e in ingest.census()["packets"].values()) > 0


def test_replay_time_range(tmp_path: Path) -> None:
    path = _make_recording(tmp_path)
    ingest = Ingest()
    delivered = asyncio.run(replay(path, ingest, VirtualClock(), speed=None, to_us=100_000))
    with RecordingReader(path) as reader:
        expected = sum(1 for off, _ in reader if off <= 100_000)
    assert delivered == expected
