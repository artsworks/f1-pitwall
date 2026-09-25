"""Time-constant EMAs. Weight = 1 - exp(-dt/tau) with dt from game session-time
deltas, so 20 Hz and 60 Hz converge to the same value (docs/02)."""

from __future__ import annotations

import math

from pitwall.protocol.layouts import Corners


class Ema:
    __slots__ = ("tau", "value", "last_t")

    def __init__(self, tau: float) -> None:
        self.tau = tau
        self.value: float | None = None
        self.last_t: float | None = None

    def update(self, t: float, x: float) -> float:
        if self.value is None or self.last_t is None:
            self.value = x
        else:
            w = 1.0 - math.exp(-(t - self.last_t) / self.tau)
            self.value += w * (x - self.value)
        self.last_t = t
        return self.value

    def reset(self) -> None:
        self.value = None
        self.last_t = None


class CornersEma:
    """Four independent EMAs over a Corners value."""

    __slots__ = ("emas",)

    def __init__(self, tau: float) -> None:
        self.emas = Corners(Ema(tau), Ema(tau), Ema(tau), Ema(tau))

    def update(self, t: float, x: Corners) -> Corners:
        return Corners(
            self.emas.rl.update(t, x.rl),
            self.emas.rr.update(t, x.rr),
            self.emas.fl.update(t, x.fl),
            self.emas.fr.update(t, x.fr),
        )

    def reset(self) -> None:
        for e in self.emas.as_tuple():
            e.reset()
