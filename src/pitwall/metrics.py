"""Rolling latency metrics: trigger->speak and packet->snapshot."""

from __future__ import annotations

import statistics
from collections import deque


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
        self.trigger_to_speak_ms = _Window(window)
        self.packet_to_snapshot_ms = _Window(window)

    def note_trigger_to_speak(self, trigger_t: float, spoken_t: float) -> None:
        self.trigger_to_speak_ms.add((spoken_t - trigger_t) * 1000.0)

    def note_packet_to_snapshot(self, packet_t: float, snapshot_t: float) -> None:
        self.packet_to_snapshot_ms.add((snapshot_t - packet_t) * 1000.0)

    def summary(self) -> dict[str, dict[str, float]]:
        return {
            "trigger_to_speak_ms": {
                "p50": self.trigger_to_speak_ms.percentile(50),
                "p99": self.trigger_to_speak_ms.percentile(99),
                "mean": self.trigger_to_speak_ms.mean(),
            },
            "packet_to_snapshot_ms": {
                "p50": self.packet_to_snapshot_ms.percentile(50),
                "p99": self.packet_to_snapshot_ms.percentile(99),
                "mean": self.packet_to_snapshot_ms.mean(),
            },
        }
