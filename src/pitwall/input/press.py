"""Press classification (docs/12): down/up edges -> ack / neg / bookmark.

```
down ─┬─ held ≥ long_ms ──────────────▶ bookmark (release ignored)
      └─ up ─▶ wait double_ms ─┬─ down ─▶ neg (3 presses still neg)
                               └─ timeout ▶ ack
```

Downs closer than `bounce_ms` to the previous down are contact bounce, and
repeated downs with no up between them (keyboard auto-repeat) are ignored.
`edge()` handles edges; `tick()` resolves the long-press and double windows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Press:
    kind: str  # "ack" | "neg" | "bookmark" | "silent"
    t: float


_IDLE = 0
_DOWN = 1
_HELD = 2  # bookmark emitted; release ignored
_AWAITING = 3  # released; waiting for a second press or the timeout


class PressDetector:
    def __init__(self, double_ms: int = 350, long_ms: int = 800, bounce_ms: int = 60) -> None:
        self.double_s = double_ms / 1000.0
        self.long_s = long_ms / 1000.0
        self.bounce_s = bounce_ms / 1000.0
        self._state = _IDLE
        self._down_t = 0.0
        self._release_t = 0.0
        self._last_down_t: float | None = None
        self._neg_emitted = False

    def edge(self, t: float, down: bool) -> Press | None:
        if down:
            if self._last_down_t is not None and t - self._last_down_t < self.bounce_s:
                return None  # contact bounce
            if self._state in (_DOWN, _HELD):
                return None  # auto-repeat or second contact without an up
            self._last_down_t = t
            if self._state == _AWAITING:
                if not self._neg_emitted:
                    self._neg_emitted = True
                    self._state = _DOWN
                    self._down_t = t
                    return Press("neg", t)
            self._state = _DOWN
            self._down_t = t
            return None
        # release edge
        if self._state == _HELD:
            self._state = _IDLE
            self._neg_emitted = False
            return None
        if self._state == _DOWN:
            self._state = _AWAITING
            self._release_t = t
        return None

    def tick(self, t: float) -> Press | None:
        if self._state == _DOWN and t - self._down_t >= self.long_s:
            self._state = _HELD
            return Press("bookmark", t)
        if self._state == _AWAITING and t - self._release_t >= self.double_s:
            self._state = _IDLE
            if self._neg_emitted:
                # Sequence already produced a negative (3 presses still neg).
                self._neg_emitted = False
                return None
            return Press("ack", t)
        return None
