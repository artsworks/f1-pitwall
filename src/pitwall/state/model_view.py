"""ModelView: model outputs the Engine computes and SessionState carries
into the Snapshot (docs/18). Plain floats/ints/strs so rules and JSON can
use them."""

from __future__ import annotations

import math
from dataclasses import dataclass

from pitwall.strategy.plans import StrategyPlan


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
    pit_window_start: int = 0
    pit_window_end: int = 0
    undercut_s: float = 0.0
    overcut_s: float = 0.0
    plans: tuple[StrategyPlan, ...] = ()
    active_plan: str = ""
    on_plan: bool = True
    plan_label: str = ""
    plan_spoken: str = ""
    plan_stops_left: int = 0
    plan_target_lap: int = 0
    plan_window_start: int = 0
    plan_window_end: int = 0
    plan_window_text: str = ""
    plan_window_open: bool = False
    plan_next_compound: str = ""
    plan_off_s: float = 0.0
    plan_switch_count: int = 0
    plan_switched_from: str = ""
    plan_switch_reason: str = ""
    plan_switch_lap: int = 0
    plan_target_shift: int = 0
    plan_b_spoken: str = ""
    plan_b_delta_s: float = 0.0
    plan_c_spoken: str = ""
    plan_c_delta_s: float = 0.0
