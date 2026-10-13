"""Rolling queue, speech, and packet latency metrics."""

from __future__ import annotations

import statistics
from collections import OrderedDict, deque
from collections.abc import Callable
from threading import Lock
from typing import Any


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


class Metrics:
    def __init__(self, window: int = 512) -> None:
        self.queue_wait_ms = _Window(window)
        self.tts_ms = _Window(window)
        self.packet_to_snapshot_ms = _Window(window)
        self.packet_to_ws_ms = _Window(window)
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
        }
