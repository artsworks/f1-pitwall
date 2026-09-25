"""Qualifying helpers: clean-air release window, lap projection, cut-off and
abort advice (docs/03). Pure functions called from SessionState.snapshot();
rules read the resulting snapshot fields."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pitwall.protocol.enums import DriverStatus, PitStatus, ResultStatus

_ON_TRACK = (DriverStatus.FLYING_LAP, DriverStatus.IN_LAP, DriverStatus.OUT_LAP)
_ACTIVE_RESULT = (ResultStatus.ACTIVE, ResultStatus.FINISHED)

# Sprint shootout session types reuse the Q1/Q2/Q3 elimination counts.
_SHOOTOUT_TO_QUALI = {10: 5, 11: 6, 12: 7}


@dataclass(frozen=True, slots=True)
class ReleaseWindow:
    gap_ahead_s: float
    gap_behind_s: float
    clean: bool
    wait_s: float  # smallest delay for a clean window; inf if none inside horizon
    cars_on_track: int


def release_window(
    cars: Sequence[Any],
    player_idx: int,
    track_length_m: float,
    pit_exit_m: float,
    out_lap_s: float,
    field_best_laps: Sequence[int],
    fallback_lap_s: float,
    clean_gap_s: float,
    horizon_s: float = 60.0,
    step_s: float = 1.0,
) -> ReleaseWindow:
    """Project every on-track car forward past the player's out-lap + a delay
    and find the first delay where pit exit has clean air both ways."""
    on_track = [
        (i, c)
        for i, c in enumerate(cars)
        if i != player_idx
        and c.driver_status in _ON_TRACK
        and c.pit_status == PitStatus.NONE
        and c.result_status in _ACTIVE_RESULT
    ]
    lap_m = track_length_m if track_length_m > 0 else 1.0
    player_best = field_best_laps[player_idx] if 0 <= player_idx < len(field_best_laps) else 0
    player_lap_s = player_best / 1000.0 if player_best > 0 else fallback_lap_s
    v_player = lap_m / player_lap_s

    def gaps(delay: float) -> tuple[float, float]:
        ahead = math.inf
        behind = math.inf
        for i, car in on_track:
            best = field_best_laps[i] if 0 <= i < len(field_best_laps) else 0
            lap_s = best / 1000.0 if best > 0 else fallback_lap_s
            v = lap_m / lap_s
            pos = (car.lap_distance + v * (out_lap_s + delay)) % lap_m
            delta_ahead = (pos - pit_exit_m) % lap_m  # car already past the exit
            delta_behind = (pit_exit_m - pos) % lap_m  # car approaching the exit
            ahead = min(ahead, delta_ahead / v_player)
            behind = min(behind, delta_behind / v)
        return ahead, behind

    gap_ahead, gap_behind = gaps(0.0)

    def clean(d: float) -> bool:
        a, b = gaps(d)
        return a >= clean_gap_s and b >= clean_gap_s

    wait = math.inf
    d = 0.0
    while d <= horizon_s:
        if clean(d):
            wait = d
            break
        d += step_s
    return ReleaseWindow(
        gap_ahead_s=gap_ahead,
        gap_behind_s=gap_behind,
        clean=gap_ahead >= clean_gap_s and gap_behind >= clean_gap_s,
        wait_s=wait,
        cars_on_track=len(on_track),
    )


def projected_lap_ms(
    sector: int,
    current_lap_time_ms: int,
    sector1_ms: int,
    sector2_ms: int,
    best_s1: int,
    best_s2: int,
    best_s3: int,
) -> int:
    """Projected final lap time: completed sectors at their actual times,
    remaining sectors at best, plus whatever the in-progress sector is over
    its best. 0 if any needed best is unknown."""
    bests = [best_s1, best_s2, best_s3]
    if sector < 0 or sector > 2:
        return 0
    needed = bests[sector:]
    if any(b <= 0 for b in needed):
        return 0
    completed = (sector1_ms if sector >= 1 else 0) + (sector2_ms if sector >= 2 else 0)
    elapsed_in_sector = max(0, current_lap_time_ms - completed)
    overrun = max(0, elapsed_in_sector - bests[sector])
    return completed + sum(needed) + overrun


def quali_cutoff_ms(
    field_best_laps: Sequence[int],
    num_active_cars: int,
    session_type: int,
    eliminated: Mapping[int, int],
) -> int:
    """Lap time of the car currently on the elimination cut-off position;
    0 when the session does not eliminate or too few laps are set."""
    st = _SHOOTOUT_TO_QUALI.get(session_type, session_type)
    elim = eliminated.get(st, 0)
    if elim <= 0:
        return 0
    cutoff_pos = num_active_cars - elim
    times = sorted(t for t in field_best_laps if t > 0)
    if cutoff_pos <= 0 or len(times) < cutoff_pos:
        return 0
    return times[cutoff_pos - 1]


@dataclass(frozen=True, slots=True)
class AbortAdvice:
    deficit_ms: int
    advised: bool
    reason: str


def abort_advice(
    projected_ms: int,
    cutoff_ms: int,
    player_best_ms: int,
    sector: int,
    fresh_sets_current: int,
    ers_store_pct: float,
    *,
    safe_margin_ms: int = 300,
    deficit_ms: int = 400,
    ers_keep_pct: float = 60.0,
) -> AbortAdvice:
    """Advise aborting a flying lap when the projection is meaningfully over
    the cut-off. Fresh sets to save and ERS in the bank adjust the margin."""
    if player_best_ms > 0 and cutoff_ms > 0 and player_best_ms < cutoff_ms - safe_margin_ms:
        return AbortAdvice(deficit_ms=0, advised=False, reason="through")
    if cutoff_ms <= 0 or projected_ms <= 0:
        return AbortAdvice(deficit_ms=0, advised=False, reason="no_cutoff")
    deficit = projected_ms - cutoff_ms
    margin = float(deficit_ms)
    reason = "deficit"
    if fresh_sets_current == 0:
        margin *= 3
        reason = "no_fresh_sets"
    if ers_store_pct >= ers_keep_pct:
        margin *= 0.7
    advised = sector >= 1 and deficit > margin
    return AbortAdvice(deficit, advised, "deficit" if advised else reason)
