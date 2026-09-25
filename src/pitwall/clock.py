"""Clock abstraction. Everything time-dependent reads a Clock so replay is
deterministic: wall clock live, virtual clock in replay/tests."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds, monotonic for WallClock, virtual time for VirtualClock."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class WallClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class VirtualClock:
    """Manual clock: sleep() advances the clock and returns immediately."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    async def sleep(self, seconds: float) -> None:
        self._t += max(0.0, seconds)


class ReplayClock:
    """Record-time clock: now() advances at `speed`x real time from `start`;
    sleep(s) waits s/speed real seconds. Keeps every timestamp in the replay
    path (recv_time, ticks, latency metrics) in one domain."""

    def __init__(self, speed: float, start: float = 0.0) -> None:
        self._speed = speed
        self._start = start
        self._mono0 = time.monotonic()

    def now(self) -> float:
        return self._start + (time.monotonic() - self._mono0) * self._speed

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self._speed)
