"""M3 model layer (docs/18): pace/degradation fit, pit loss, fuel/energy
budgets. Pure functions; thresholds arrive as arguments, state lives in
SQLite."""

from pitwall.model.budget import EnergyBudget, FuelBudget, energy_budget, fuel_budget
from pitwall.model.deg import (
    DegFit,
    Prior,
    fit_stint,
    laps_of_pace,
    resolve_prior,
    rival_pace_ms,
)
from pitwall.model.pitloss import PitLoss, current_pit_loss, measure

__all__ = [
    "DegFit",
    "EnergyBudget",
    "FuelBudget",
    "PitLoss",
    "Prior",
    "current_pit_loss",
    "energy_budget",
    "fit_stint",
    "fuel_budget",
    "laps_of_pace",
    "measure",
    "resolve_prior",
    "rival_pace_ms",
]
