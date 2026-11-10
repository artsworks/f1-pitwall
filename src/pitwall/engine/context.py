"""Shared dependencies and outputs for the engine components."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import Clock
from pitwall.config.loader import ConfigStore
from pitwall.config.thresholds import threshold
from pitwall.input.menu import DriverMenu
from pitwall.model.budget import EnergyBudget, FuelBudget
from pitwall.model.deg import DegFit, Prior
from pitwall.protocol.enums import session_kind
from pitwall.state.session import SessionState, Snapshot
from pitwall.store.db import LapRow
from pitwall.store.uids import is_synthetic_uid
from pitwall.strategy.pitwindow import NO_PLAN, PitPlan
from pitwall.voice.channel import VoiceChannel


@dataclass
class EngineContext:
    store: ConfigStore
    clock: Clock
    state: SessionState
    dispatcher: Dispatcher
    db: Any = None
    driver_db: Any = None
    synthetic_source: bool = False
    recovering: bool = False
    deg_fit: DegFit | None = None
    fresh_prior: DegFit | None = None
    pit_loss_prior: Prior | None = None
    pit_plan: PitPlan = NO_PLAN
    fuel_budget: FuelBudget | None = None
    energy_budget: EnergyBudget | None = None
    energy_prev_lap: EnergyBudget | None = None
    green_pit_loss_s: float = 0.0
    sc_pit_loss_s: float = 0.0
    green_pit_samples_s: list[float] = field(default_factory=list)
    stint_laps: list[LapRow] = field(default_factory=list)
    stint_fuel_ref: float = 0.0
    driver_rating: tuple[float, float, str] = (0.5, 0.0, "")
    mindset_override: str | None = None
    budget_live: float | None = None
    page: str = "race"
    menu: DriverMenu = field(default_factory=DriverMenu)
    opinions: dict[str, tuple[str, int]] = field(default_factory=dict)
    voice: VoiceChannel | None = None
    on_voice_event: Callable[[dict[str, Any]], None] | None = None
    on_session_end: Callable[[int], None] | None = None
    last_snapshot: Snapshot | None = None
    mode_source: Callable[[], dict[str, Any]] = field(default=lambda: {})

    def th(self, name: str, default: float) -> float:
        return threshold(self.store.current().thresholds, name, default)

    def mode(self) -> dict[str, Any]:
        return self.mode_source()

    def synthetic_session(self, uid: int | None = None) -> bool:
        session_uid = self.state.session_uid if uid is None else uid
        return self.synthetic_source or (session_uid is not None and is_synthetic_uid(session_uid))

    def race_laps(self) -> int:
        kind = session_kind(self.state.session_type)
        return self.state.total_laps if kind == "race" and self.state.total_laps > 0 else 0
