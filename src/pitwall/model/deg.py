"""Pace + degradation model (docs/18). Ordinary least squares on valid,
non-SC laps of `lap_time_ms = base + deg*tyre_age - fuel*fuel_burned_laps`;
normal equations solved by hand (no numpy dep)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pitwall.protocol.packets import SessionHistoryPacket
    from pitwall.store.db import Database, LapRow


_FUEL_SPAN_MIN_LAPS = 0.5
_FUEL_AGE_COLLINEAR_R = 0.98


@dataclass(frozen=True, slots=True)
class DegFit:
    base_ms: float  # pace at tyre_age 0, fuel normalised to fuel_ref
    deg_ms_per_lap: float  # linear degradation slope
    fuel_ms_per_lap: float  # pace gained per lap of fuel burned (>= 0)
    n: int  # valid laps used
    rmse_ms: float
    confidence: float  # 0..1
    source: str  # 'fit' | 'prior' | 'blend'
    fuel_fitted: bool = False  # fuel slope estimated from this data, not the prior
    deg2_ms_per_lap2: float = 0.0
    deg_sd_ms_per_lap: float = 0.0
    deg_posterior_sd_ms_per_lap: float = 0.0


@dataclass(frozen=True, slots=True)
class Prior:
    value: float
    weight: float
    source: str  # 'learned' | 'overlay' | 'default' | 'session'


DEG_FUEL_REF = "deg_fuel_ref_ms_per_lap"


def fuel_adjusted_deg(deg: float, deg_fuel_ref: float | None, fuel_now: float) -> float:
    """Learned deg re-split with today's fuel slope. A stint fit only sees the
    lap-time slope (deg - fuel), so its deg is tied to the fuel slope it
    assumed; `deg_fuel_ref` stores that assumption."""
    if deg_fuel_ref is None:
        return deg
    return max(0.0, deg + fuel_now - deg_fuel_ref)


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


def _normal_matrix(rows: list[list[float]]) -> list[list[float]]:
    n = len(rows[0])
    ata = [[0.0] * n for _ in range(n)]
    for row in rows:
        for i in range(n):
            for j in range(n):
                ata[i][j] += row[i] * row[j]
    return ata


def _ols(rows: list[list[float]], ys: list[float]) -> list[float] | None:
    ata = _normal_matrix(rows)
    n = len(rows[0])
    aty = [0.0] * n
    for row, y in zip(rows, ys, strict=True):
        for i in range(n):
            aty[i] += row[i] * y
    return _solve(ata, aty)


def fuel_burned_laps(laps: Sequence[LapRow]) -> list[float]:
    """Laps' worth of fuel burned since the stint's heaviest lap, one per lap.

    Uses the recorded fuel kg scaled by the stint's own burn per lap. The
    game's `fuel_remaining_laps` is the MFD margin at the flag (flat over a
    stint), so it is only a fallback for rows without kg."""
    if not laps:
        return []
    if all(lap.fuel_kg > 0 for lap in laps):
        hi = max(laps, key=lambda lap: lap.fuel_kg)
        lo = min(laps, key=lambda lap: lap.fuel_kg)
        span = lo.lap_num - hi.lap_num
        if span <= 0 or hi.fuel_kg <= lo.fuel_kg:
            return [0.0] * len(laps)
        per_lap = (hi.fuel_kg - lo.fuel_kg) / span
        return [(hi.fuel_kg - lap.fuel_kg) / per_lap for lap in laps]
    ref = max(lap.fuel_remaining_laps for lap in laps)
    return [ref - lap.fuel_remaining_laps for lap in laps]


def _correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def session_base_ms(laps: Sequence[LapRow], deg_ms_per_lap: float) -> float:
    """Median clean-lap time this session with tyre age taken out; 0 if no clean lap."""
    usable = [lap for lap in laps if lap.valid == 1 and lap.sc_status == 0 and lap.lap_time_ms > 0]
    if not usable:
        return 0.0
    return float(median(lap.lap_time_ms - deg_ms_per_lap * lap.tyre_age_laps for lap in usable))


def fit_stint(
    laps: Sequence[LapRow],
    prior: DegFit,
    *,
    min_laps: int,
    fuel_coeff_fixed: float | None,
    deg_max_ms_per_lap: float = 600.0,
    deg_rmse_bad_ms: float = 800.0,
    thermal_hi_c: float | None = None,
    quadratic: bool = False,
    quad_min_laps: int = 8,
    quad_min_t: float = 2.0,
) -> DegFit:
    """OLS refit of a stint's valid laps; blend with the prior while short."""
    usable = [
        lap
        for lap in laps
        if lap.valid == 1
        and lap.sc_status == 0
        and lap.lap_time_ms > 0
        and getattr(lap, "traffic", 0) != 1
        and (thermal_hi_c is None or lap.tyre_inner_c == 0 or lap.tyre_inner_c <= thermal_hi_c)
    ]
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

    # Fuel and tyre age both rise one per lap in a stint; when they are
    # collinear the fuel slope comes from the prior, not the fit.
    burned = fuel_burned_laps(usable)
    ages = [float(lap.tyre_age_laps) for lap in usable]
    if max(burned) < _FUEL_SPAN_MIN_LAPS or abs(_correlation(ages, burned)) > _FUEL_AGE_COLLINEAR_R:
        fuel_coeff_fixed = prior.fuel_ms_per_lap if fuel_coeff_fixed is None else fuel_coeff_fixed
    rows: list[list[float]] = []
    ys: list[float] = []
    for lap, b in zip(usable, burned, strict=True):
        if fuel_coeff_fixed is not None:
            rows.append([1.0, float(lap.tyre_age_laps)])
            ys.append(lap.lap_time_ms + fuel_coeff_fixed * b)
        else:
            rows.append([1.0, float(lap.tyre_age_laps), -b])
            ys.append(float(lap.lap_time_ms))

    coeff = _ols(rows, ys)
    fuel_fitted = coeff is not None and fuel_coeff_fixed is None
    if coeff is None:
        # Collinear (e.g. all tyre_age equal): fall back to deg-only fit.
        mean_y = sum(ys) / len(ys)
        mean_a = sum(lap.tyre_age_laps for lap in usable) / n
        var = sum((lap.tyre_age_laps - mean_a) ** 2 for lap in usable)
        if var < 1e-9:
            return DegFit(mean_y, 0.0, prior.fuel_ms_per_lap, n, 0.0, 0.5, "fit")
        deg = sum((a - mean_a) * (y - mean_y) for a, y in zip(ages, ys, strict=True)) / var
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
            (lap.lap_time_ms - (base + deg * lap.tyre_age_laps - fuel * b)) ** 2
            for lap, b in zip(usable, burned, strict=True)
        )
        / n
    )
    mean_age = sum(ages) / n
    age_ss = sum((age - mean_age) ** 2 for age in ages)
    deg_sd = rmse / math.sqrt(age_ss) if age_ss > 0 else 0.0
    deg_posterior_sd = deg_sd
    source = "fit"
    if n < 2 * min_laps:
        w = (n - min_laps + 1) / (min_laps + 1)
        base = w * base + (1 - w) * prior.base_ms
        deg = w * deg + (1 - w) * prior.deg_ms_per_lap
        fuel = w * fuel + (1 - w) * prior.fuel_ms_per_lap
        deg_posterior_sd = deg_sd / max(w, 0.2)
        source = "blend"
    confidence = min(max(n / (2 * min_laps), 0.0), 1.0) * min(
        max(1.0 - rmse / deg_rmse_bad_ms, 0.2), 1.0
    )
    if quadratic and source == "fit" and n >= quad_min_laps and len(set(ages)) >= 4:
        quad_rows = [[1.0, age, age**2] for age in ages]
        quad_ys = [float(lap.lap_time_ms) + fuel * b for lap, b in zip(usable, burned, strict=True)]
        quad_coeff = _ols(quad_rows, quad_ys)
        if quad_coeff is not None:
            quad_base, quad_deg, quad_deg2 = quad_coeff
            quad_rmse = math.sqrt(
                sum(
                    (y - (quad_base + quad_deg * age + quad_deg2 * age**2)) ** 2
                    for age, y in zip(ages, quad_ys, strict=True)
                )
                / n
            )
            inv_col = _solve(_normal_matrix(quad_rows), [0.0, 0.0, 1.0])
            if inv_col is not None and inv_col[2] >= 0:
                quad_se = quad_rmse * math.sqrt(inv_col[2])
                slope_col = _solve(_normal_matrix(quad_rows), [0.0, 1.0, 0.0])
                quad_slope_sd = (
                    quad_rmse * math.sqrt(slope_col[1])
                    if slope_col is not None and slope_col[1] >= 0
                    else deg_sd
                )
                if (
                    quad_deg2 > 0
                    and quad_deg >= 0
                    and quad_deg + 2 * quad_deg2 * max(ages) <= deg_max_ms_per_lap
                    and quad_deg2 >= quad_min_t * quad_se
                ):
                    quad_confidence = min(max(n / (2 * min_laps), 0.0), 1.0) * min(
                        max(1.0 - quad_rmse / deg_rmse_bad_ms, 0.2), 1.0
                    )
                    return DegFit(
                        quad_base,
                        quad_deg,
                        fuel,
                        n,
                        quad_rmse,
                        quad_confidence,
                        "fit",
                        fuel_fitted,
                        quad_deg2,
                        deg_sd,
                        deg_posterior_sd_ms_per_lap=quad_slope_sd,
                    )
    return DegFit(
        base,
        deg,
        fuel,
        n,
        rmse,
        confidence,
        source,
        fuel_fitted,
        0.0,
        deg_sd,
        deg_posterior_sd_ms_per_lap=deg_posterior_sd,
    )


def cliff_age(fit: DegFit, cliff_ms: float) -> float:
    """Tyre age where linear and quadratic degradation reaches the cliff."""
    if fit.deg2_ms_per_lap2 > 0:
        return (
            -fit.deg_ms_per_lap
            + math.sqrt(fit.deg_ms_per_lap**2 + 4 * fit.deg2_ms_per_lap2 * cliff_ms)
        ) / (2 * fit.deg2_ms_per_lap2)
    if fit.deg_ms_per_lap > 0:
        return cliff_ms / fit.deg_ms_per_lap
    return math.inf


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
    pace_laps = cliff_age(fit, cliff_ms) - tyre_age
    wear_laps = math.inf if wear_per_lap <= 0 else (wear_cliff_pct - wear_pct) / wear_per_lap
    return max(0.0, min(pace_laps, wear_laps))


def scoped(name: str, race_laps: int) -> str:
    """model_params name for a stint-derived value: races learn per race
    distance (tyre wear scales with it), other sessions learn unscoped."""
    return f"{name}@{race_laps}L" if race_laps > 0 else name


def corner_wear_life(
    wear: Sequence[float],
    start_wear: Sequence[float],
    laps_run: float,
    *,
    wear_cliff_pct: float,
    default_rate_pct: float,
) -> float:
    """Laps until the worst corner reaches the wear cliff, each corner at its
    own measured rate this stint (the default rate until a lap is run)."""
    life = math.inf
    for now, start in zip(wear, start_wear, strict=True):
        rate = (now - start) / laps_run if laps_run >= 1 and now > start else default_rate_pct
        if rate > 0:
            life = min(life, (wear_cliff_pct - now) / rate)
    return max(0.0, life)


def planning_fit(fit: DegFit, prior: DegFit, rmse_bad_ms: float) -> DegFit:
    """The stint fit the strategy planner uses: the slope shrunk to the prior
    as the fit error approaches `rmse_bad_ms`, so noisy laps cannot swing
    the plan while a clean steep fit is taken at face value."""
    if fit.source == "prior" or rmse_bad_ms <= 0:
        return fit
    w = min(max(1.0 - fit.rmse_ms / rmse_bad_ms, 0.0), 1.0)
    return replace(
        fit,
        deg_ms_per_lap=w * fit.deg_ms_per_lap + (1 - w) * prior.deg_ms_per_lap,
        deg2_ms_per_lap2=w * fit.deg2_ms_per_lap2,
    )


def fit_is_clean(
    fit: DegFit,
    *,
    deg_max_ms_per_lap: float,
    deg_rmse_bad_ms: float,
    base_min_ms: float,
    base_max_ms: float,
) -> bool:
    """A stint fit good enough to become a learned prior: a real OLS fit, low error,
    slope inside the clamp and a plausible lap time."""
    return (
        fit.source == "fit"
        and fit.rmse_ms <= deg_rmse_bad_ms
        and 0 < fit.deg_ms_per_lap < deg_max_ms_per_lap
        and 0 <= fit.fuel_ms_per_lap < deg_max_ms_per_lap
        and base_min_ms <= fit.base_ms <= base_max_ms
    )


def rival_pace_ms(history: SessionHistoryPacket, window: int, outlier_ratio: float = 0.0) -> int:
    """Median of the last `window` valid laps in a Session History packet.

    With `outlier_ratio` > 0, laps slower than ratio x the car's best valid lap
    (red-flag, safety-car, pit and formation laps) are left out."""
    laps = history.laps[: history.num_laps]
    times = [
        lap.lap_time_ms for lap in laps if lap.lap_valid_bit_flags & 0x01 and lap.lap_time_ms > 0
    ]
    if times and outlier_ratio > 0:
        limit = min(times) * outlier_ratio
        times = [t for t in times if t <= limit]
    if not times:
        return 0
    return int(median(times[-window:]))
