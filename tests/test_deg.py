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
    planning_fit,
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
    assert fit.deg_posterior_sd_ms_per_lap == fit.deg_sd_ms_per_lap


def test_traffic_laps_are_excluded() -> None:
    laps = [_lap(a, 90_000 + 100 * a) for a in range(8)]
    laps[3] = replace(laps[3], lap_time_ms=laps[3].lap_time_ms + 800, traffic=1)
    laps[6] = replace(laps[6], lap_time_ms=laps[6].lap_time_ms + 800, traffic=1)

    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)

    assert fit.n == 6
    assert fit.deg_ms_per_lap == pytest.approx(100.0)


def test_overheated_laps_are_excluded() -> None:
    laps = [
        replace(
            _lap(age, 90_000 + 100 * age + (50 * (age - 5) ** 2 if age >= 6 else 0)),
            tyre_inner_c=110.0 if age >= 6 else 95.0,
        )
        for age in range(10)
    ]

    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0, thermal_hi_c=100.0)
    unfiltered = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)

    assert fit.n == 6
    assert fit.deg_ms_per_lap == pytest.approx(100.0)
    assert unfiltered.deg_ms_per_lap > fit.deg_ms_per_lap + 50.0


def test_quadratic_fit_captures_accelerating_degradation() -> None:
    laps = [_lap(age, 90_000 + 20 * age + 6 * age**2) for age in range(12)]

    quadratic = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0, quadratic=True)
    linear = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)

    assert quadratic.deg2_ms_per_lap2 == pytest.approx(6.0, abs=0.5)
    assert quadratic.deg_ms_per_lap == pytest.approx(20.0, abs=0.5)
    assert laps_of_pace(
        quadratic,
        0,
        0.0,
        cliff_ms=1500,
        wear_cliff_pct=70.0,
        wear_per_lap=0.0,
    ) < laps_of_pace(
        linear,
        0,
        0.0,
        cliff_ms=1500,
        wear_cliff_pct=70.0,
        wear_per_lap=0.0,
    )


def test_quadratic_fit_falls_back_for_linear_data_with_noise() -> None:
    noise = [0, 2, -2, 1, -1, 0, 1, -2, 2, -1, 0, 1]
    laps = [_lap(age, 90_000 + 100 * age + noise[age]) for age in range(len(noise))]

    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0, quadratic=True)

    assert fit.deg2_ms_per_lap2 == 0.0


def test_real_fit_reports_slope_error_and_planning_scales_quadratic_term() -> None:
    noise = [0, 2, -2, 1, -1, 0, 1, -2]
    laps = [_lap(age, 90_000 + 100 * age + noise[age]) for age in range(len(noise))]
    fit = fit_stint(laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)
    prior = replace(PRIOR, deg2_ms_per_lap2=4.0)

    age_mean = sum(range(len(noise))) / len(noise)
    age_ss = sum((age - age_mean) ** 2 for age in range(len(noise)))
    planned = planning_fit(replace(fit, deg2_ms_per_lap2=4.0), prior, rmse_bad_ms=1000.0)

    assert fit.deg_sd_ms_per_lap == pytest.approx(fit.rmse_ms / age_ss**0.5)
    assert fit.deg_posterior_sd_ms_per_lap == fit.deg_sd_ms_per_lap > 0.0
    assert planned.deg_posterior_sd_ms_per_lap == fit.deg_posterior_sd_ms_per_lap
    assert planned.deg2_ms_per_lap2 == pytest.approx(4.0 * (1.0 - fit.rmse_ms / 1000.0))


def test_cliff_age_closed_form() -> None:
    from pitwall.model.deg import cliff_age

    quadratic = DegFit(90_000.0, 20.0, 30.0, 12, 0.0, 1.0, "fit", deg2_ms_per_lap2=6.0)
    linear = DegFit(90_000.0, 100.0, 30.0, 12, 0.0, 1.0, "fit")
    flat = DegFit(90_000.0, 0.0, 30.0, 12, 0.0, 1.0, "fit")

    assert cliff_age(quadratic, 1500.0) == pytest.approx(
        (-20.0 + (20.0**2 + 4 * 6.0 * 1500.0) ** 0.5) / 12.0
    )
    assert cliff_age(linear, 1500.0) == 15.0
    assert cliff_age(flat, 1500.0) == float("inf")


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
    assert fit.deg_posterior_sd_ms_per_lap == pytest.approx(fit.deg_sd_ms_per_lap / 0.5)


def test_full_fit_and_blend_keep_their_slope_uncertainty() -> None:
    noise = [0, 3, -2, 1, -1, 4]
    full_laps = [_lap(age, 90_000 + 100 * age + noise[age]) for age in range(6)]
    full = fit_stint(full_laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)

    blend_laps = full_laps[:-1]
    blend = fit_stint(blend_laps, PRIOR, min_laps=3, fuel_coeff_fixed=30.0)
    w = (5 - 3 + 1) / (3 + 1)

    assert full.n == 2 * 3 and full.source == "fit"
    assert full.deg_posterior_sd_ms_per_lap == full.deg_sd_ms_per_lap > 0.0
    assert blend.n == 2 * 3 - 1 and blend.source == "blend"
    assert blend.deg_posterior_sd_ms_per_lap == pytest.approx(blend.deg_sd_ms_per_lap / max(w, 0.2))


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
