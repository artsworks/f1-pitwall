"""Deg fit: OLS recovers a synthetic slope, prior/blend behaviour, laps of
pace, rival pace median."""

from __future__ import annotations

from dataclasses import replace
from statistics import median
from types import SimpleNamespace
from typing import Any

import pytest

from pitwall.model.deg import (
    DegFit,
    fit_stint,
    fuel_adjusted_deg,
    fuel_burned_laps,
    laps_of_pace,
    rival_pace_ms,
    session_base_ms,
)
from pitwall.store.db import LapRow

PRIOR = DegFit(90_000.0, 80.0, 30.0, 0, 0.0, 0.3, "prior")


def _lap(
    age: int, time_ms: float, *, valid: int = 1, sc: int = 0, fuel_rem: float = 20.0
) -> LapRow:
    return LapRow(
        id=age,
        session_uid=1,
        car_idx=0,
        lap_num=age + 1,
        lap_time_ms=int(time_ms),
        s1_ms=0,
        s2_ms=0,
        compound=18,
        tyre_age_laps=age,
        fuel_remaining_laps=fuel_rem,
        valid=valid,
        invalid_reasons=() if valid else ("invalid",),
        wear_pct=0.0,
        fuel_kg=0.0,
        ers_deployed_j=0.0,
        sc_status=sc,
        weather=0,
    )


def _history(times: list[tuple[int, int]]) -> Any:
    laps = [SimpleNamespace(lap_time_ms=t, lap_valid_bit_flags=flags) for t, flags in times]
    return SimpleNamespace(laps=laps, num_laps=len(laps))


def test_fit_recovers_known_slope() -> None:
    laps = [_lap(a, 90_000 + 100 * a) for a in range(8)]
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)
    assert fit.source == "fit"
    assert abs(fit.deg_ms_per_lap - 100.0) < 1.0
    assert abs(fit.base_ms - 90_000.0) < 1.0
    assert fit.rmse_ms < 1.0 and fit.n == 8
    assert fit.confidence == 1.0


def test_fuel_slope_fitted_when_not_fixed() -> None:
    # time = base + deg*age - fuel_gain*fuel_burned (lighter is faster); quadratic
    # burn keeps the fuel regressor independent of tyre_age.
    laps = [
        _lap(a, 90_000 + 50 * a - 25 * (a * a * 0.1), fuel_rem=5.0 - a * a * 0.1) for a in range(8)
    ]
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=None)
    assert abs(fit.deg_ms_per_lap - 50.0) < 1.0
    assert abs(fit.fuel_ms_per_lap - 25.0) < 1.0


def test_under_min_laps_returns_prior() -> None:
    laps = [_lap(a, 90_000 + 100 * a) for a in range(2)]
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=None)
    assert fit.source == "prior" and fit.deg_ms_per_lap == 80.0


def test_blend_weights() -> None:
    # n=4, min=3 -> w = (4-3+1)/(3+1) = 0.5 between OLS(100) and prior(80)
    laps = [_lap(a, 90_000 + 100 * a) for a in range(4)]
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)
    assert fit.source == "blend"
    assert abs(fit.deg_ms_per_lap - 90.0) < 1.0


def test_invalid_and_sc_laps_excluded() -> None:
    laps = [_lap(a, 90_000 + 100 * a) for a in range(3)]
    laps += [_lap(3, 120_000, valid=0), _lap(4, 120_000, sc=0)]
    laps[4] = _lap(4, 120_000, sc=1)
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)
    assert fit.n == 3 and fit.source == "blend"


def test_session_base_uses_median_of_clean_laps() -> None:
    clean = [_lap(1, 112_618), _lap(2, 112_958), _lap(3, 112_093)]
    laps = [*clean, _lap(4, 127_038, valid=0), _lap(5, 127_038, sc=1)]

    expected = median(lap.lap_time_ms - 60 * lap.tyre_age_laps for lap in clean)

    assert session_base_ms(laps, 60) == expected


def test_session_base_returns_zero_without_clean_laps() -> None:
    laps = [_lap(1, 127_038, valid=0), _lap(2, 127_038, sc=1)]

    assert session_base_ms(laps, 60) == 0.0


def test_deg_slope_clamped() -> None:
    laps = [_lap(a, 90_000 + 5000 * a) for a in range(6)]
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=0.0, deg_max_ms_per_lap=600)
    assert fit.deg_ms_per_lap == 600.0


def test_laps_of_pace_both_branches() -> None:
    fit = DegFit(90_000.0, 100.0, 30.0, 8, 50.0, 1.0, "fit")
    # deg-limited: (1500/100 - 5) = 10 < wear branch
    assert (
        laps_of_pace(
            fit, tyre_age=5, wear_pct=10.0, cliff_ms=1500, wear_cliff_pct=70, wear_per_lap=2.5
        )
        == 10.0
    )
    # wear-limited: (70-60)/2.5 = 4 < deg branch
    assert (
        laps_of_pace(
            fit, tyre_age=5, wear_pct=60.0, cliff_ms=1500, wear_cliff_pct=70, wear_per_lap=2.5
        )
        == 4.0
    )
    # never negative
    assert (
        laps_of_pace(
            fit, tyre_age=20, wear_pct=90.0, cliff_ms=1500, wear_cliff_pct=70, wear_per_lap=2.5
        )
        == 0.0
    )


def test_rival_pace_median() -> None:
    h = _history([(90_000, 1), (91_000, 1), (95_000, 1), (99_000, 1), (120_000, 0)])
    assert rival_pace_ms(h, 3) == 95_000  # last 3 valid: 91000, 95000, 99000
    assert rival_pace_ms(_history([(90_000, 0)]), 3) == 0


def test_rival_pace_drops_red_flag_laps() -> None:
    h = _history([(95_000, 1), (233_000, 1), (236_000, 1)])
    assert rival_pace_ms(h, 3) == 233_000
    assert rival_pace_ms(h, 3, outlier_ratio=1.07) == 95_000


def test_fuel_burn_from_kg_not_mfd_margin() -> None:
    # Game MFD margin stays flat; kg falls 1.5/lap. Lighter car gains 30 ms/lap
    # of fuel, so the raw slope (70) understates tyre deg (100).
    laps = [
        replace(_lap(a, 90_000 + 100 * a - 30 * a, fuel_rem=0.4), fuel_kg=20.0 - 1.5 * a)
        for a in range(8)
    ]
    assert fuel_burned_laps(laps)[-1] == pytest.approx(7.0)
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=None)
    assert fit.fuel_ms_per_lap == PRIOR.fuel_ms_per_lap == 30.0
    assert fit.deg_ms_per_lap == pytest.approx(100.0, abs=1.0)


def test_learned_deg_resplit_with_current_fuel_slope() -> None:
    # A race stint fit with fuel fixed at 30 read flat laps as 30 deg; once
    # fuel is learned as 0, the same laps mean 0 deg.
    assert fuel_adjusted_deg(30.0, 30.0, 0.0) == 0.0
    assert fuel_adjusted_deg(80.0, 30.0, 50.0) == 100.0
    assert fuel_adjusted_deg(80.0, None, 0.0) == 80.0
