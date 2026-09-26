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


_GROUPS = (
    (("fl", "fr", "rl", "rr"), "all round"),
    (("fl", "fr"), "fronts"),
    (("rl", "rr"), "rears"),
    (("fl", "rl"), "lefts"),
    (("fr", "rr"), "rights"),
)


def _group(keys: list[str]) -> list[tuple[str, list[str]]]:
    """Largest named groups first: all round, axles, sides, then single corners."""
    left = list(keys)
    out: list[tuple[str, list[str]]] = []
    for ks, label in _GROUPS:
        if all(k in left for k in ks):
            out.append((label, list(ks)))
            left = [k for k in left if k not in ks]
    out.extend((dict(_ORDER)[k], [k]) for k in left)
    return out


def pressure_text(calls: tuple[PressureCall, ...]) -> str:
    """Short spoken advice: "rights down 0.2. Lefts are already at the minimum",
    "up 0.4 all round, rear right just 0.2"."""
    moves = [c for c in calls if not c.limited]
    limited = [c for c in calls if c.limited]
    parts: list[str] = []
    if len(moves) == 4 and len({c.delta_psi for c in moves}) == 2:
        amounts = [c.delta_psi for c in moves]
        common = max(set(amounts), key=amounts.count)
        odd = [c for c in moves if c.delta_psi != common]
        if len(odd) == 1 and (odd[0].delta_psi > 0) == (common > 0):
            verb = "up" if common > 0 else "down"
            odd_psi = abs(odd[0].delta_psi)
            parts.append(f"{verb} {abs(common):.1f} all round, {odd[0].name} just {odd_psi:.1f}")
            moves = []
    by_delta: dict[float, list[str]] = {}
    for c in moves:
        by_delta.setdefault(c.delta_psi, []).append(c.corner)
    for delta, keys in by_delta.items():
        verb = "up" if delta > 0 else "down"
        for label, _ in _group(keys):
            if label == "all round":
                parts.append(f"{verb} {abs(delta):.1f} all round")
            else:
                parts.append(f"{label} {verb} {abs(delta):.1f}")
    text = ", ".join(parts)
    notes: list[str] = []
    for edge in ("minimum", "maximum"):
        keys = [c.corner for c in limited if (c.wanted_psi < 0) == (edge == "minimum")]
        for label, ks in _group(keys):
            if label == "all round":
                notes.append(f"already at the {edge} all round")
            else:
                notes.append(f"{label} {'are' if len(ks) > 1 else 'is'} already at the {edge}")
    if not notes:
        return text
    note = ", ".join(notes)
    if not text:
        return note
    return f"{text}. {note[0].upper()}{note[1:]}"
