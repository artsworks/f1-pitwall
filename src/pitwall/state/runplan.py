"""Qualifying run plan: push again, cool, box, or push now (docs/17).

`RunTracker` follows the player's laps within a run (line crossings, hot vs
cool laps, per-lap mistakes); `run_plan` is the pure decision taken at each
line crossing of a hot lap."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Lap kinds within a run.
OUT = "out"
HOT = "hot"
COOL = "cool"


@dataclass(frozen=True, slots=True)
class HotLap:
    """Summary of one completed push lap."""

    lap_time_ms: int
    s1_ms: int
    s2_ms: int
    s3_ms: int
    ers_start_pct: float
    ers_end_pct: float
    lockups: int
    spins: int
    invalid: bool


@dataclass(frozen=True, slots=True)
class Plan:
    plan: str  # "push" | "cool" | "box" | "push_now" | ""
    reason: str  # "ready" | "battery" | "tyres" | "safe" | "time" | "fuel" | "flag"


def run_plan(
    *,
    margin_ms: int,
    margin_kind: str,
    safe_margin_ms: int,
    ers_pct: float,
    ers_min_pct: float,
    hottest_c: float,
    tyre_hot_c: float,
    time_left_s: float,
    cool_lap_s: float,
    fuel_laps: float,
    fuel_push_laps: float = 2.0,
    fuel_cool_laps: float = 3.0,
) -> Plan:
    """Decision on crossing the line after a hot lap.

    Cooling needs time for a cool lap before the flag and fuel for cool + hot
    + in lap; pushing needs fuel for hot + in lap. Outside the cut with no
    time or fuel to cool, keep pushing on whatever battery is left."""
    if time_left_s <= 0:
        return Plan("box", "flag")
    if margin_kind and margin_ms >= safe_margin_ms:
        return Plan("box", "safe")
    if fuel_laps < fuel_push_laps:
        return Plan("box", "fuel")
    need = "battery" if ers_pct < ers_min_pct else "tyres" if hottest_c >= tyre_hot_c else ""
    if not need:
        return Plan("push", "ready")
    if time_left_s < cool_lap_s:
        return Plan("push_now", "time")
    if fuel_laps < fuel_cool_laps:
        return Plan("push_now", "fuel")
    return Plan("cool", need)


def mistakes_text(lap: HotLap, best_s: tuple[int, int, int]) -> str:
    """Spoken summary of what went wrong on a hot lap, '' if it was clean."""
    parts: list[str] = []
    if lap.lockups:
        parts.append("a lock-up" if lap.lockups == 1 else f"{lap.lockups} lock-ups")
    if lap.spins:
        parts.append("a spin" if lap.spins == 1 else f"{lap.spins} spins")
    if lap.invalid:
        parts.append("lap invalid")
    mine_s = (lap.s1_ms, lap.s2_ms, lap.s3_ms)
    lost = [
        (mine - best, i + 1)
        for i, (mine, best) in enumerate(zip(mine_s, best_s, strict=True))
        if mine > 0 and best > 0
    ]
    if lost:
        worst_ms, sector = max(lost)
        if worst_ms >= 100:
            parts.append(f"{worst_ms / 1000:.1f} lost in sector {sector} to your best")
    return ", ".join(parts)


class RunTracker:
    """Line crossings and lap kinds during a qualifying run.

    The game keeps `driver_status` on flying lap across consecutive laps, so
    cool laps are tracked here: a lap starts as the plan says, and the sector
    1 time overrides it (slower than best by `cool_pace_pct` = cool)."""

    __slots__ = (
        "kind",
        "crossings",
        "last_hot",
        "plan",
        "_prev_lap_ms",
        "_prev_sector",
        "_s1",
        "_s2",
        "_ers_start",
        "_lockups0",
        "_spins0",
        "_invalid",
        "_lap_start_t",
        "cool_start_t",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.kind = ""
        self.crossings = 0
        self.last_hot: HotLap | None = None
        self.plan = Plan("", "")
        self._prev_lap_ms: int | None = None
        self._prev_sector = 0
        self._s1 = self._s2 = 0
        self._ers_start = 0.0
        self._lockups0 = self._spins0 = 0
        self._invalid = False
        self._lap_start_t = 0.0
        self.cool_start_t = 0.0

    def note_rewind(self) -> None:
        self._prev_lap_ms = None

    def _start_lap(self, t: float, kind: str, ers: float, lockups: int, spins: int) -> None:
        self.kind = kind
        self._s1 = self._s2 = 0
        self._ers_start = ers
        self._lockups0 = lockups
        self._spins0 = spins
        self._invalid = False
        self._lap_start_t = t
        if kind == COOL:
            self.cool_start_t = t

    def update(
        self,
        *,
        t: float,
        phase: str,
        lap_time_ms: int,
        lap_distance: float,
        sector: int,
        sector1_ms: int,
        sector2_ms: int,
        invalid: bool,
        ers_pct: float,
        lockups: int,
        spins: int,
        best_s1_ms: int,
        cool_pace_pct: float,
        decide: Callable[[], Plan],
        extend_cool: bool = False,
    ) -> HotLap | None:
        """Feed one Lap Data tick. `decide` is a zero-arg callable returning the
        Plan for the line just crossed after a hot lap; `extend_cool` keeps a
        cool lap's plan for another cool lap. Returns the HotLap
        completed on this tick, if any."""
        prev_ms, self._prev_lap_ms = self._prev_lap_ms, lap_time_ms
        prev_sector, self._prev_sector = self._prev_sector, sector
        if phase == "out_lap":
            if self.kind != OUT:
                self.kind = OUT
                self.plan = Plan("", "")
            return None
        if phase != "flying":
            if self.kind:
                self.kind = ""
                self.plan = Plan("", "")
            return None
        self._invalid |= invalid
        if self.kind in ("", OUT):
            self._start_lap(t, HOT, ers_pct, lockups, spins)
            self.crossings += 1
            return None
        crossed = prev_ms is not None and lap_time_ms + 5000 < prev_ms and lap_distance < 500
        if crossed:
            assert prev_ms is not None
            done: HotLap | None = None
            self.crossings += 1
            if self.kind == HOT:
                done = HotLap(
                    lap_time_ms=prev_ms,
                    s1_ms=self._s1,
                    s2_ms=self._s2,
                    s3_ms=max(0, prev_ms - self._s1 - self._s2) if self._s1 and self._s2 else 0,
                    ers_start_pct=self._ers_start,
                    ers_end_pct=ers_pct,
                    lockups=lockups - self._lockups0,
                    spins=spins - self._spins0,
                    invalid=self._invalid,
                )
                self.last_hot = done
                self.plan = decide()
            elif not (self.kind == COOL and extend_cool):
                self.plan = Plan("", "")
            self._start_lap(t, COOL if self.plan.plan == "cool" else HOT, ers_pct, lockups, spins)
            return done
        if sector == 1 and prev_sector == 0 and sector1_ms > 0:
            self._s1 = sector1_ms
            if best_s1_ms > 0:
                slow = sector1_ms > best_s1_ms * (1 + cool_pace_pct / 100)
                new = COOL if slow else HOT
                if new != self.kind:
                    self.kind = new
                    if new == COOL:
                        self.cool_start_t = t
        elif sector == 2 and prev_sector == 1 and sector2_ms > 0:
            self._s2 = sector2_ms
        return None
