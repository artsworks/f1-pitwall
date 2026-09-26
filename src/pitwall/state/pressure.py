"""Tyre pressure advice from carcass temperatures over a run's flying laps.

Each corner's time-averaged inner temperature is compared with a target
window; the distance outside it maps to a small/medium/large pressure step.
`hot_sign` sets the direction: +1 means a hot tyre wants more pressure (the
F1 games: lower pressure runs hotter). Targets are clamped to the setup's
pressure range; a corner already at its limit is reported as `limited`."""

from __future__ import annotations

from dataclasses import dataclass

from pitwall.protocol.layouts import Corners

_ORDER = (("fl", "front left"), ("fr", "front right"), ("rl", "rear left"), ("rr", "rear right"))


@dataclass(frozen=True, slots=True)
class PressureCall:
    corner: str  # "fl" | "fr" | "rl" | "rr"
    name: str
    avg_c: float
    size: str  # "small" | "medium" | "large"
    delta_psi: float  # signed change to make
    target_psi: float  # current + delta; 0 when the setup pressure is unknown
    limited: bool = False  # already at the setup range limit in that direction
    wanted_psi: float = 0.0  # the change before clamping to the setup range


class RunTemps:
    """Time-weighted mean of inner tyre temps while flying, reset per run."""

    __slots__ = ("_sum", "_last_t", "seconds")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._sum = [0.0, 0.0, 0.0, 0.0]
        self._last_t: float | None = None
        self.seconds = 0.0

    def update(self, t: float, temps: Corners, active: bool, max_dt: float = 0.5) -> None:
        last, self._last_t = self._last_t, t
        if not active or last is None:
            return
        dt = t - last
        if dt <= 0 or dt > max_dt:
            return
        for i, v in enumerate(temps.as_tuple()):
            self._sum[i] += float(v) * dt
        self.seconds += dt

    def mean(self) -> Corners:
        if self.seconds <= 0:
            return Corners(0.0, 0.0, 0.0, 0.0)
        rl, rr, fl, fr = (s / self.seconds for s in self._sum)
        return Corners(rl, rr, fl, fr)


def pressure_advice(
    avg: Corners,
    current_psi: Corners,
    low_c: float,
    high_c: float,
    *,
    hot_sign: float = -1.0,
    medium_c: float = 5.0,
    large_c: float = 10.0,
    steps_psi: tuple[float, float, float] = (0.2, 0.4, 0.8),
    front_range: tuple[float, float] | None = None,
    rear_range: tuple[float, float] | None = None,
) -> tuple[PressureCall, ...]:
    out: list[PressureCall] = []
    for key, name in _ORDER:
        t = float(getattr(avg, key))
        if t <= 0:
            continue
        if t > high_c:
            off, sign = t - high_c, hot_sign
        elif t < low_c:
            off, sign = low_c - t, -hot_sign
        else:
            continue
        idx = 0 if off < medium_c else 1 if off < large_c else 2
        delta = round((1.0 if sign > 0 else -1.0) * steps_psi[idx], 1)
        wanted = delta
        cur = float(getattr(current_psi, key))
        rng = front_range if key.startswith("f") else rear_range
        target = round(cur + delta, 1) if cur > 0 else 0.0
        limited = False
        if cur > 0 and rng is not None:
            lo, hi = rng
            target = round(min(max(target, lo), hi), 1)
            delta = round(target - cur, 1)
            limited = delta == 0
        out.append(
            PressureCall(
                corner=key,
                name=name,
                avg_c=round(t, 1),
                size=("small", "medium", "large")[idx],
                delta_psi=delta,
                target_psi=target,
                limited=limited,
                wanted_psi=wanted,
            )
        )
    return tuple(out)


def pressure_text(calls: tuple[PressureCall, ...]) -> str:
    """Spoken advice, grouping an axle (or all four) that wants the same change:
    "drop the fronts 0.4, raise the rear right 0.2"."""
    by = {c.corner: c for c in calls}

    def same(keys: tuple[str, ...]) -> bool:
        cs = [by.get(k) for k in keys]
        return all(cs) and len({(c.delta_psi, c.limited) for c in cs if c}) == 1

    groups: list[tuple[str, PressureCall]] = []
    if len(by) == 4 and same(("fl", "fr", "rl", "rr")):
        groups.append(("all four", by["fl"]))
    else:
        for keys, label in ((("fl", "fr"), "the fronts"), (("rl", "rr"), "the rears")):
            if same(keys):
                groups.append((label, by[keys[0]]))
            else:
                groups.extend((f"the {by[k].name}", by[k]) for k in keys if k in by)
    parts: list[str] = []
    for label, c in groups:
        if c.limited:
            plural = label in ("all four", "the fronts", "the rears")
            edge = "minimum" if c.wanted_psi < 0 else "maximum"
            parts.append(f"{label} {'are' if plural else 'is'} already at the {edge}")
        else:
            verb = "raise" if c.delta_psi > 0 else "drop"
            parts.append(f"{verb} {label} {abs(c.delta_psi):.1f}")
    return ", ".join(parts)
