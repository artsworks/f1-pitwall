"""Replay: stream a recording through the same PacketSink as the live socket.

recv_time passed to the sink is always the record's own timestamp in seconds,
so ingest sees identical data at any speed. speed=None means "as fast as
possible" (no sleeps); a numeric speed paces via the Clock (a VirtualClock's
sleep returns instantly, so tests stay deterministic).
"""

from __future__ import annotations

from pathlib import Path

from pitwall.clock import Clock
from pitwall.net.recording import RecordingReader
from pitwall.net.udp import PacketSink


async def replay(
    path: Path,
    sink: PacketSink,
    clock: Clock,
    speed: float | None = None,
    *,
    from_us: int | None = None,
    to_us: int | None = None,
) -> int:
    """Feed records to sink. Returns the number of datagrams delivered."""
    delivered = 0
    start = clock.now()
    with RecordingReader(path) as reader:
        for offset_us, payload in reader:
            if from_us is not None and offset_us < from_us:
                continue
            if to_us is not None and offset_us > to_us:
                break
            t = offset_us / 1_000_000
            if speed is not None:
                target = start + t / speed
                delay = target - clock.now()
                if delay > 0:
                    await clock.sleep(delay)
            sink.on_datagram(payload, t)
            delivered += 1
    return delivered
