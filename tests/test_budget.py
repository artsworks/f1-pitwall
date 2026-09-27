"""Fuel margin sign and energy-budget modes."""

from __future__ import annotations

import math

from pitwall.model.budget import energy_budget, fuel_budget


def test_fuel_margin_sign() -> None:
    enough = fuel_budget(laps_remaining=10, fuel_in_tank_kg=20.0, per_lap_kg=1.7, source="learned")
    assert enough.fuel_laps == 20.0 / 1.7
    assert enough.margin_laps > 0
    short = fuel_budget(laps_remaining=15, fuel_in_tank_kg=20.0, per_lap_kg=1.7, source="overlay")
    assert short.margin_laps < 0
    assert short.source == "overlay"


def test_energy_over_budget() -> None:
    b = energy_budget(
        store_j=4_000_000.0,
        store_capacity_j=4_000_000.0,
        laps_remaining=10,
        deployed_this_lap_j=900_000.0,
        harvested_this_lap_j=0.0,
        over_tolerance_j=200_000.0,
    )
    assert b.per_lap_j == 400_000.0
    assert b.lap_delta_j == 500_000.0
    assert b.mode == "over"
    assert b.laps_to_floor == 4_000_000.0 / 900_000.0


def test_energy_under_budget() -> None:
    b = energy_budget(
        store_j=4_000_000.0,
        store_capacity_j=4_000_000.0,
        laps_remaining=10,
        deployed_this_lap_j=100_000.0,
        harvested_this_lap_j=0.0,
        over_tolerance_j=200_000.0,
    )
    assert b.lap_delta_j == -300_000.0 and b.mode == "under"


def test_energy_on_budget_and_attack_ok() -> None:
    on = energy_budget(
        store_j=4_000_000.0,
        store_capacity_j=4_000_000.0,
        laps_remaining=10,
        deployed_this_lap_j=400_000.0,
        harvested_this_lap_j=0.0,
        over_tolerance_j=200_000.0,
    )
    assert on.mode == "on_budget"
    # lots of charge left vs laps remaining and policy wants attack
    atk = energy_budget(
        store_j=3_900_000.0,
        store_capacity_j=4_000_000.0,
        laps_remaining=5,
        deployed_this_lap_j=600_000.0,
        harvested_this_lap_j=0.0,
        over_tolerance_j=200_000.0,
        attack_ok=True,
    )
    assert atk.mode == "attack_ok"  # laps_to_floor ~7.8 > 5+1
    assert atk.store_pct == 97.5


def test_energy_not_draining_is_inf() -> None:
    b = energy_budget(
        store_j=2_000_000.0,
        store_capacity_j=4_000_000.0,
        laps_remaining=5,
        deployed_this_lap_j=0.0,
        harvested_this_lap_j=400_000.0,
        over_tolerance_j=200_000.0,
    )
    assert math.isinf(b.laps_to_floor)
