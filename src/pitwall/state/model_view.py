"""ModelView: model outputs the Engine computes and SessionState carries
into the Snapshot (docs/18). Plain floats/ints/strs so rules and JSON can
use them. pit_plan_* fields hold neutral defaults until H3's optimiser."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelView:
    deg_fit_source: str = ""
    deg_ms_per_lap: float = 0.0
    deg_confidence: float = 0.0
    base_pace_ms: float = 0.0
    laps_of_pace: float = math.inf
    wear_per_lap_pct: float = 0.0
    pit_loss_s: float = 0.0
    pit_loss_source: str = ""
    fuel_margin_laps: float = 0.0
    fuel_per_lap_kg: float = 0.0
    fuel_source: str = ""
    energy_per_lap_mj: float = 0.0
    energy_lap_delta_mj: float = 0.0
    energy_laps_to_floor: float = math.inf
    energy_mode: str = ""
    predicted_lap_ms: int = 0
    pit_plan: str = ""
    pit_plan_lap: int = 0
    pit_plan_gain_s: float = 0.0
    pit_plan_confidence: float = 0.0
    pit_plan_risk: float = 0.0
    pit_plan_rival_idx: int = -1
    pit_plan_rival_name: str = ""
    pit_plan_reason: str = ""
