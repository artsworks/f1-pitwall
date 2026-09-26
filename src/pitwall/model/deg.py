"""Pace + degradation model (docs/18). Ordinary least squares on valid,
non-SC laps of `lap_time_ms = base + deg*tyre_age + fuel*(fuel_ref -
fuel_remaining)`; normal equations solved by hand (no numpy dep)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pitwall.protocol.packets import SessionHistoryPacket
    from pitwall.store.db import Database, LapRow


_FUEL_SPAN_MIN_LAPS = 0.5


@dataclass(frozen=True, slots=True)
class DegFit:
    base_ms: float  # pace at tyre_age 0, fuel normalised to fuel_ref
    deg_ms_per_lap: float  # linear degradation slope
    fuel_ms_per_lap: float  # pace gained per lap of fuel burned (>= 0)
    n: int  # valid laps used
    rmse_ms: float
    confidence: float  # 0..1
    source: str  # 'fit' | 'prior' | 'blend'


@dataclass(frozen=True, slots=True)
class Prior:
    value: float
    weight: float
    source: str  # 'learned' | 'overlay' | 'default' | 'session'


def resolve_prior(
    db: Database | None,
    track_id: int,
    compound: int,
    name: str,
    *,
    overlay_value: float | None = None,
    default: float = 0.0,
    min_weight: float = 2.0,
) -> Prior:
    """model_params (weight >= min_weight) -> overlay -> default."""
    if db is not None:
        param = db.get_param(track_id, compound, name)
        if param is not None and param.weight >= min_weight:
            return Prior(param.value, param.weight, "learned")
    if overlay_value is not None:
        return Prior(overlay_value, 0.0, "overlay")
    return Prior(default, 0.0, "default")


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination on the n x n normal equations; None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _ols(rows: list[list[float]], ys: list[float]) -> list[float] | None:
    n = len(rows[0])
    ata = [[0.0] * n for _ in range(n)]
    aty = [0.0] * n
    for row, y in zip(rows, ys, strict=True):
        for i in range(n):
            aty[i] += row[i] * y
            for j in range(n):
                ata[i][j] += row[i] * row[j]
    return _solve(ata, aty)


def fit_stint(
    laps: Sequence[LapRow],
    prior: DegFit,
    *,
    min_laps: int,
    fuel_coeff_fixed: float | None,
    deg_max_ms_per_lap: float = 600.0,
    deg_rmse_bad_ms: float = 800.0,
) -> DegFit:
    """OLS refit of a stint's valid laps; blend with the prior while short."""
    usable = [lap for lap in laps if lap.valid == 1 and lap.sc_status == 0 and lap.lap_time_ms > 0]
    n = len(usable)
    if n < min_laps:
        return DegFit(
            prior.base_ms,
            prior.deg_ms_per_lap,
            prior.fuel_ms_per_lap,
            n,
            prior.rmse_ms,
            prior.confidence,
            "prior",
        )

    # fuel term: laps' worth of fuel burned relative to the stint's first
    # observed fuel level (fuel_ref).
    fuel_ref = max(lap.fuel_remaining_laps for lap in usable)
    if fuel_ref - min(lap.fuel_remaining_laps for lap in usable) < _FUEL_SPAN_MIN_LAPS:
        fuel_coeff_fixed = prior.fuel_ms_per_lap if fuel_coeff_fixed is None else fuel_coeff_fixed
    rows: list[list[float]] = []
    ys: list[float] = []
    for lap in usable:
        burned = fuel_ref - lap.fuel_remaining_laps
        if fuel_coeff_fixed is not None:
            rows.append([1.0, float(lap.tyre_age_laps)])
            ys.append(lap.lap_time_ms - fuel_coeff_fixed * burned)
        else:
            rows.append([1.0, float(lap.tyre_age_laps), burned])
            ys.append(float(lap.lap_time_ms))

    coeff = _ols(rows, ys)
    if coeff is None:
        # Collinear (e.g. all tyre_age equal): fall back to deg-only fit.
        mean_y = sum(ys) / len(ys)
        mean_a = sum(lap.tyre_age_laps for lap in usable) / n
        var = sum((lap.tyre_age_laps - mean_a) ** 2 for lap in usable)
        if var < 1e-9:
            return DegFit(mean_y, 0.0, prior.fuel_ms_per_lap, n, 0.0, 0.5, "fit")
        deg = sum((lap.tyre_age_laps - mean_a) * (lap.lap_time_ms - mean_y) for lap in usable) / var
        coeff = [mean_y - deg * mean_a, deg] + ([] if fuel_coeff_fixed is not None else [0.0])

    base = coeff[0]
    deg = min(max(coeff[1], 0.0), deg_max_ms_per_lap)
    fuel = (
        fuel_coeff_fixed
        if fuel_coeff_fixed is not None
        else min(max(coeff[2], 0.0), deg_max_ms_per_lap)
    )

    rmse = math.sqrt(
        sum(
            (
                lap.lap_time_ms
                - (base + deg * lap.tyre_age_laps + fuel * (fuel_ref - lap.fuel_remaining_laps))
            )
            ** 2
            for lap in usable
        )
        / n
    )
    source = "fit"
    if n < 2 * min_laps:
        w = (n - min_laps + 1) / (min_laps + 1)
        base = w * base + (1 - w) * prior.base_ms
        deg = w * deg + (1 - w) * prior.deg_ms_per_lap
        fuel = w * fuel + (1 - w) * prior.fuel_ms_per_lap
        source = "blend"
    confidence = min(max(n / (2 * min_laps), 0.0), 1.0) * min(
        max(1.0 - rmse / deg_rmse_bad_ms, 0.2), 1.0
    )
    return DegFit(base, deg, fuel, n, rmse, confidence, source)


def laps_of_pace(
    fit: DegFit,
    tyre_age: int,
    wear_pct: float,
    *,
    cliff_ms: float,
    wear_cliff_pct: float,
    wear_per_lap: float,
) -> float:
    """Laps until the tyre is cliff_ms slower than at age 0 or wear crosses
    the wear cliff, whichever is sooner. >= 0; inf when the tyre never deg."""
    pace_laps = math.inf if fit.deg_ms_per_lap <= 0 else cliff_ms / fit.deg_ms_per_lap - tyre_age
    wear_laps = math.inf if wear_per_lap <= 0 else (wear_cliff_pct - wear_pct) / wear_per_lap
    return max(0.0, min(pace_laps, wear_laps))


def rival_pace_ms(history: SessionHistoryPacket, window: int) -> int:
    """Median of the last `window` valid laps in a Session History packet."""
    laps = history.laps[: history.num_laps]
    times = [
        lap.lap_time_ms for lap in laps if lap.lap_valid_bit_flags & 0x01 and lap.lap_time_ms > 0
    ]
    if not times:
        return 0
    return int(median(times[-window:]))
