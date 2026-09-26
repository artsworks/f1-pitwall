"""Fuel and 2026 energy per-lap budgets (docs/18). Pure functions; every
threshold/mindset value arrives as an explicit argument."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FuelBudget:
    laps_remaining: int
    fuel_laps: float
    margin_laps: float  # fuel_laps - laps_remaining; negative = short
    per_lap_kg: float
    source: str  # 'learned' | 'overlay' | 'default' | 'session'


@dataclass(frozen=True, slots=True)
class EnergyBudget:
    per_lap_j: float  # allowance per remaining lap, floored at 0
    deployed_this_lap_j: float
    harvested_this_lap_j: float
    lap_delta_j: float  # deployed - harvested - per_lap_j; >0 = over budget
    store_pct: float
    soc_floor_pct: float
    laps_to_floor: float  # laps until the store hits the floor (inf if not draining)
    mode: str  # 'on_budget' | 'over' | 'under' | 'attack_ok'


def fuel_budget(
    *,
    laps_remaining: int,
    fuel_in_tank_kg: float,
    per_lap_kg: float,
    source: str,
) -> FuelBudget:
    fuel_laps = fuel_in_tank_kg / per_lap_kg if per_lap_kg > 0 else math.inf
    return FuelBudget(
        laps_remaining=laps_remaining,
        fuel_laps=fuel_laps,
        margin_laps=fuel_laps - laps_remaining,
        per_lap_kg=per_lap_kg,
        source=source,
    )


def energy_budget(
    *,
    store_j: float,
    store_capacity_j: float,
    laps_remaining: int,
    deployed_this_lap_j: float,
    harvested_this_lap_j: float,
    harvest_limit_per_lap_j: float = 0.0,
    soc_floor_pct: float = 0.0,
    over_tolerance_j: float = 200_000.0,
    attack_ok: bool = False,
) -> EnergyBudget:
    floor_j = store_capacity_j * soc_floor_pct / 100.0
    per_lap_j = max(0.0, store_j - floor_j) / max(1, laps_remaining) + harvest_limit_per_lap_j
    lap_delta_j = deployed_this_lap_j - harvested_this_lap_j - per_lap_j
    net_drain_j = deployed_this_lap_j - harvested_this_lap_j - harvest_limit_per_lap_j
    laps_to_floor = (
        (store_j - floor_j) / net_drain_j if net_drain_j > 0 and store_j > floor_j else math.inf
    )
    if lap_delta_j > over_tolerance_j:
        mode = "over"
    elif lap_delta_j < -over_tolerance_j:
        mode = "under"
    elif attack_ok and laps_to_floor > laps_remaining + 1:
        mode = "attack_ok"
    else:
        mode = "on_budget"
    store_pct = min(100.0, store_j / store_capacity_j * 100.0) if store_capacity_j > 0 else 0.0
    return EnergyBudget(
        per_lap_j=per_lap_j,
        deployed_this_lap_j=deployed_this_lap_j,
        harvested_this_lap_j=harvested_this_lap_j,
        lap_delta_j=lap_delta_j,
        store_pct=store_pct,
        soc_floor_pct=soc_floor_pct,
        laps_to_floor=laps_to_floor,
        mode=mode,
    )
