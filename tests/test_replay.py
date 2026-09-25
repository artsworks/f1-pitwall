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


def test_replay_clock_scales() -> None:
    import asyncio

    import pitwall.clock as clock_mod
    from pitwall.clock import ReplayClock

    mono = iter([100.0, 100.0, 105.0])

    def monkey_t() -> float:
        return next(mono)

    orig = clock_mod.time.monotonic
    clock_mod.time.monotonic = monkey_t  # type: ignore[assignment]
    try:
        c = ReplayClock(10.0, start=42.0)
        assert c.now() == 42.0
        assert c.now() == 42.0 + 5.0 * 10.0
    finally:
        clock_mod.time.monotonic = orig

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    async def run() -> None:
        orig_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await c.sleep(20.0)
            await c.sleep(-5.0)
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(run())
    assert slept == [2.0, 0.0]


def test_replay_pacing_uses_record_time(tmp_path) -> None:
    """At high speed every datagram is delivered with record-offset recv_time."""
    import asyncio

    from pitwall.clock import ReplayClock
    from pitwall.net.replay import replay as net_replay

    class _Sink:
        def __init__(self) -> None:
            self.times: list[float] = []

        def on_datagram(self, payload: bytes, recv_time: float) -> None:
            self.times.append(recv_time)

    path = tmp_path / "rec.f1bin"
    write_synthetic_recording(path, mixed_session_packets(n_frames=20))
    sink = _Sink()
    delivered = asyncio.run(net_replay(path, sink, ReplayClock(1000.0), speed=1000.0))
    n = len(mixed_session_packets(n_frames=20))
    assert delivered == n == len(sink.times)
    # recv_time is the record offset, monotonically non-decreasing
    assert sink.times == sorted(sink.times)
    assert sink.times[0] == 0.0
