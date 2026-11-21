"""Rolling queue, speech, packet latency and process-budget metrics."""

from __future__ import annotations

import os
import statistics
import sys
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from threading import Lock
from typing import Any


def _default_rss_mb() -> float:
    """Resident set of this process in MB. Same approach as voice/spike.py
    on Windows; ru_maxrss elsewhere (peak RSS, close enough for a budget)."""
    if sys.platform == "win32":
        import win32api  # type: ignore[import-untyped]
        import win32process  # type: ignore[import-untyped]

        m = win32process.GetProcessMemoryInfo(win32api.GetCurrentProcess())
        return float(m["WorkingSetSize"]) / 2**20
    import resource

    rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return rss / 2**20
    return rss / 2**10


class _Window:
    def __init__(self, n: int = 512) -> None:
        self.samples: deque[float] = deque(maxlen=n)

    def add(self, v: float) -> None:
        self.samples.append(v)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        data = sorted(self.samples)
        k = max(0, min(len(data) - 1, round(p / 100 * (len(data) - 1))))
        return data[k]

    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else 0.0

    def max(self) -> float:
        return max(self.samples) if self.samples else 0.0


class Metrics:
    def __init__(
        self,
        window: int = 512,
        *,
        process_time: Callable[[], float] = time.process_time,
        rss_reader: Callable[[], float] = _default_rss_mb,
        cpu_count: int | None = None,
    ) -> None:
        self.queue_wait_ms = _Window(window)
        self.tts_ms = _Window(window)
        self.packet_to_snapshot_ms = _Window(window)
        self.packet_to_ws_ms = _Window(window)
        self.tick_ms = _Window(window)
        self.parse_us = _Window(window)
        self.cpu_pct = 0.0
        self.rss_mb = 0.0
        self._process_time = process_time
        self._rss_reader = rss_reader
        self._cpu_count = cpu_count or os.cpu_count() or 1
        self._proc_wall_t: float | None = None
        self._proc_cpu_t = 0.0
        self.queue_source: Callable[[], tuple[int, float]] | None = None
        self._handoff_wall: OrderedDict[str, float] = OrderedDict()
        self._handoff_lock = Lock()

    def note_queue_wait(self, trigger_t: float, handoff_t: float) -> None:
        self.queue_wait_ms.add((handoff_t - trigger_t) * 1000.0)

    def note_handoff(self, call_id: str, wall_t: float) -> None:
        with self._handoff_lock:
            self._handoff_wall[call_id] = wall_t
            self._handoff_wall.move_to_end(call_id)
            while len(self._handoff_wall) > 256:
                self._handoff_wall.popitem(last=False)

    def note_spoken(self, call_id: str, wall_t: float) -> None:
        with self._handoff_lock:
            start = self._handoff_wall.pop(call_id, None)
        if start is not None:
            self.tts_ms.add((wall_t - start) * 1000.0)

    def forget_handoff(self, call_id: str) -> None:
        with self._handoff_lock:
            self._handoff_wall.pop(call_id, None)

    def note_packet_to_snapshot(self, packet_t: float, snapshot_t: float) -> None:
        self.packet_to_snapshot_ms.add((snapshot_t - packet_t) * 1000.0)

    def note_packet_to_ws(self, packet_t: float, sent_t: float) -> None:
        self.packet_to_ws_ms.add((sent_t - packet_t) * 1000.0)

    def note_tick(self, start_t: float, end_t: float) -> None:
        self.tick_ms.add((end_t - start_t) * 1000.0)

    def note_parse(self, start_t: float, end_t: float) -> None:
        self.parse_us.add((end_t - start_t) * 1_000_000.0)

    def sample_process(self, now: float) -> None:
        """Sample process CPU% (share of the whole machine) and RSS. Call at
        ~1 Hz; the first call only primes the deltas."""
        cpu_t = self._process_time()
        if self._proc_wall_t is not None:
            wall = now - self._proc_wall_t
            if wall > 0.0:
                self.cpu_pct = (cpu_t - self._proc_cpu_t) / wall / self._cpu_count * 100.0
        self._proc_wall_t = now
        self._proc_cpu_t = cpu_t
        self.rss_mb = self._rss_reader()

    def summary(self) -> dict[str, Any]:
        queue_count, queue_oldest_s = (
            self.queue_source() if self.queue_source is not None else (0, 0.0)
        )
        return {
            "queue_wait_ms": {
                "p50": self.queue_wait_ms.percentile(50),
                "p99": self.queue_wait_ms.percentile(99),
                "mean": self.queue_wait_ms.mean(),
            },
            "tts_ms": {
                "p50": self.tts_ms.percentile(50),
                "p99": self.tts_ms.percentile(99),
                "mean": self.tts_ms.mean(),
            },
            "voice_queue": {
                "count": queue_count,
                "oldest_s": queue_oldest_s,
            },
            "packet_to_snapshot_ms": {
                "p50": self.packet_to_snapshot_ms.percentile(50),
                "p99": self.packet_to_snapshot_ms.percentile(99),
                "mean": self.packet_to_snapshot_ms.mean(),
            },
            "packet_to_ws_ms": {
                "p50": self.packet_to_ws_ms.percentile(50),
                "p99": self.packet_to_ws_ms.percentile(99),
                "mean": self.packet_to_ws_ms.mean(),
            },
            "tick_ms": {
                "p50": self.tick_ms.percentile(50),
                "p99": self.tick_ms.percentile(99),
                "max": self.tick_ms.max(),
            },
            "parse_us": {
                "p50": self.parse_us.percentile(50),
                "p99": self.parse_us.percentile(99),
                "mean": self.parse_us.mean(),
            },
            "process": {
                "cpu_pct": self.cpu_pct,
                "rss_mb": self.rss_mb,
            },
        }
