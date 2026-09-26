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


class PausableClock:
    """Wraps a Clock so a paced replay can be paused/resumed. now() excludes
    time spent paused; sleep() waits out the pause before continuing."""

    def __init__(self, inner: Clock) -> None:
        self._inner = inner
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._event = asyncio.Event()
        self._event.set()

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._inner.now()
            self._event.clear()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._inner.now() - self._paused_at
            self._paused_at = None
            self._event.set()

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def now(self) -> float:
        t = self._inner.now() - self._paused_total
        if self._paused_at is not None:
            t -= self._inner.now() - self._paused_at
        return t

    async def sleep(self, seconds: float) -> None:
        await self._event.wait()
        await self._inner.sleep(seconds)
        await self._event.wait()
