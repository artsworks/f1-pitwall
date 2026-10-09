"""Engine: snapshot -> evaluate -> submit, driven live or by replay.

Replay ticks whenever the record clock crosses a tick boundary, so a 10x or
max-speed replay makes identical decisions to 1x (determinism per docs/07).
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import logging
import math
import sqlite3
import time
import traceback
from collections.abc import Mapping
from pathlib import Path
from statistics import median
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, CallSink, Dispatcher, LogSink
from pitwall.clock import Clock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import InputSettings, MenuItemModel, resolve_mindset
from pitwall.config.thresholds import threshold
from pitwall.derive import is_synthetic_uid
from pitwall.hindsight import grade_and_store
from pitwall.ingest import Ingest
from pitwall.input.menu import DriverMenu, ReplyPicker, answer
from pitwall.input.press import Press, PressDetector
from pitwall.metrics import Metrics
from pitwall.model.budget import EnergyBudget, FuelBudget, energy_budget, fuel_budget
from pitwall.model.deg import (
    DEG_FUEL_REF,
    DegFit,
    Prior,
    corner_wear_life,
    fit_is_clean,
    fit_stint,
    fuel_adjusted_deg,
    laps_of_pace,
    planning_fit,
    resolve_prior,
    scoped,
    session_base_ms,
)
from pitwall.model.pitloss import current_pit_loss, measure, ref_pace_after_ms, ref_pace_ms
from pitwall.net.recording import RecordingReader
from pitwall.protocol.enums import session_kind
from pitwall.rules.engine import STALENESS_DEFAULT_S, RuleEngine
from pitwall.rules.expr import expr_names, namespace_data
from pitwall.setup.advisor import SetupAdvisor
from pitwall.setup.states import majority_state
from pitwall.state.lap import LapSummary
from pitwall.state.model_view import ModelView
from pitwall.state.session import ErsLapTotals, SessionState, Snapshot
from pitwall.store.db import LapRow
from pitwall.strategy.battle import (
    HOLD,
    PASS_COMPOUND,
    PASS_NO_OT,
    PASS_OT,
    Battle,
    BattleInputs,
    BattleRates,
    BattleTracker,
    Episode,
    shrink,
)
from pitwall.strategy.pace import pace_words
from pitwall.strategy.pitwindow import NO_PLAN, PitPlan, RivalView, optimise
from pitwall.strategy.plans import (
    DRY,
    CompoundModel,
    PlanEvent,
    PlanFields,
    PlanInputs,
    PlanState,
    PlanTracker,
    view_fields,
)
from pitwall.tune import load_cooldown_mults

log = logging.getLogger(__name__)


def action_bits(inp: InputSettings) -> dict[str, int]:
    """Named UDP Action buttons beyond ack/silent (docs/12)."""
    return {
        "mindset": int(inp.mindset_toggle_bit),
        "page": int(inp.page_cycle_bit),
        "menu_up": int(inp.menu_up_bit),
        "menu_down": int(inp.menu_down_bit),
        "menu_close": int(inp.menu_close_bit),
        **{f"item:{sc.item}": int(sc.bit) for sc in inp.shortcuts},
    }


_MENU_CLIENT_OPS = {
    "up": "menu_up",
    "down": "menu_down",
    "confirm": "menu_confirm",
    "close": "menu_close",
}


def _with_plan[T: (ModelView, Snapshot)](obj: T, f: PlanFields) -> T:
    # Every PlanFields field must also exist on ModelView and Snapshot.
    return dataclasses.replace(
        obj,
        **{field.name: getattr(f, field.name) for field in dataclasses.fields(PlanFields)},
    )


_NON_GREEN_REASONS = frozenset({"first_lap", "pitted", "after_in_lap", "safety_car", "flashback"})
# Pit plans that end in a stop. "stay" and "no_stop" are plans too, but the
# wing change at the stop only makes sense when a stop is coming.
_STOP_PLANS = frozenset({"box_now", "box_in_n", "cheap_stop", "free_stop", "undercut", "overcut"})


def _rival_pace(snap: Snapshot, idx: int, closing_s: float) -> str:
    """Pace words for a rival (+ = he is faster): last lap against ours when
    both are representative, else the measured closing rate."""
    if not 0 <= idx < len(snap.cars):
        return ""
    his = snap.cars[idx].last_lap_time_ms
    if his and snap.player_last_lap_ms:
        delta_s = (snap.player_last_lap_ms - his) / 1000.0
        if abs(delta_s) <= 1.5:  # beyond that one of the laps was a pit or incident lap
            return pace_words(delta_s)
    if abs(closing_s) > 1.5:
        return ""  # a gap trend this steep spans a pass or a pit stop, not pace
    return pace_words(closing_s)


@dataclasses.dataclass(frozen=True, slots=True)
class _EnergyLapLatch:
    lap_num: int
    start_store_j: float
    start_laps_remaining: int
    gradable: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _PendingPitLoss:
    session_uid: int
    track_id: int
    in_lap: LapRow
    out_lap: LapRow
    ref_pace_ms: int
    lane_ms: int
    neutralised: int


class _EnergyLapTracker:
    def __init__(self) -> None:
        self._latch: _EnergyLapLatch | None = None
        self._finishing: _EnergyLapLatch | None = None
        self._graded_seq: int | None = None
        self._partial_next_latch = False
        self.prev_lap: EnergyBudget | None = None

    def reset(self) -> None:
        self._latch = None
        self._finishing = None
        self._graded_seq = None
        self._partial_next_latch = True
        self.prev_lap = None

    def _start_latch(
        self,
        lap_num: int,
        laps_remaining: int,
        store_j: float,
        *,
        gradable: bool,
    ) -> None:
        self._latch = _EnergyLapLatch(lap_num, store_j, laps_remaining, gradable)
        self._partial_next_latch = False

    def _grade(
        self,
        lap: _EnergyLapLatch,
        counters: tuple[float, float],
        *,
        store_j: float,
        store_capacity_j: float,
        soc_floor_pct: float,
        over_tolerance_j: float,
        attack_ok: bool,
    ) -> None:
        self.prev_lap = None
        if not lap.gradable:
            return
        deployed_j, harvested_j = counters
        self.prev_lap = energy_budget(
            store_j=store_j,
            allowance_store_j=lap.start_store_j,
            store_capacity_j=store_capacity_j,
            laps_remaining=lap.start_laps_remaining,
            deployed_this_lap_j=deployed_j,
            harvested_this_lap_j=harvested_j,
            soc_floor_pct=soc_floor_pct,
            over_tolerance_j=over_tolerance_j,
            attack_ok=attack_ok,
        )

    def observe(
        self,
        lap_num: int,
        laps_remaining: int,
        store_j: float,
        dep: float,
        harv: float,
        *,
        counters_current: bool,
        finished: ErsLapTotals | None,
        store_capacity_j: float,
        soc_floor_pct: float,
        over_tolerance_j: float,
        attack_ok: bool,
    ) -> tuple[float, float, float | None, int]:
        if lap_num < 1:
            if self._latch is not None or self._finishing is not None:
                self.reset()
            live_dep, live_harv = (dep, harv) if counters_current else (0.0, 0.0)
            return live_dep, live_harv, None, laps_remaining

        if self._latch is None:
            self._start_latch(
                lap_num,
                laps_remaining,
                store_j,
                gradable=lap_num == 1 and not self._partial_next_latch,
            )
        elif lap_num == self._latch.lap_num + 1:
            self._finishing = self._latch
            self.prev_lap = None
            self._start_latch(lap_num, laps_remaining, store_j, gradable=True)
        elif lap_num != self._latch.lap_num:
            self._finishing = None
            self.prev_lap = None
            self._start_latch(lap_num, laps_remaining, store_j, gradable=False)

        if finished is not None and finished.seq != self._graded_seq:
            finishing = self._finishing
            if finishing is not None and finished.lap_num == finishing.lap_num:
                self._grade(
                    finishing,
                    (finished.deployed_j, finished.harvested_j),
                    store_j=store_j,
                    store_capacity_j=store_capacity_j,
                    soc_floor_pct=soc_floor_pct,
                    over_tolerance_j=over_tolerance_j,
                    attack_ok=attack_ok,
                )
                self._finishing = None
            self._graded_seq = finished.seq

        return self._live_values(
            laps_remaining,
            dep,
            harv,
            counters_current=counters_current,
        )

    def _live_values(
        self,
        laps_remaining: int,
        dep: float,
        harv: float,
        *,
        counters_current: bool,
    ) -> tuple[float, float, float | None, int]:
        live_dep, live_harv = (dep, harv) if counters_current else (0.0, 0.0)
        lap = self._latch
        if lap is not None and lap.gradable:
            return live_dep, live_harv, lap.start_store_j, lap.start_laps_remaining
        return live_dep, live_harv, None, laps_remaining


class Engine:
    def __init__(
        self,
        store: ConfigStore,
        clock: Clock,
        ingest: Ingest,
        state: SessionState,
        rule_engine: RuleEngine | None,
        dispatcher: Dispatcher,
        learning_pack_dir: Path | None = None,
        learning_pack_keep_days: int = 30,
        synthetic_source: bool = False,
    ) -> None:
        self.store = store
        settings = store.current()
        self.setup_advisor = SetupAdvisor(state, settings.setup_rules)
        self.clock = clock
        self.ingest = ingest
        self.state = state
        self.synthetic_source = synthetic_source
        self.rule_engine = rule_engine
        self.dispatcher = dispatcher
        self.learning_pack_dir = learning_pack_dir
        self.learning_pack_keep_days = learning_pack_keep_days
        self.metrics = dispatcher.metrics
        self.speaker_name = "null"
        self.recording_desc = "off"
        self._paused = False
        inp = store.current().input
        self.detector = PressDetector(
            double_ms=inp.double_press_ms,
            long_ms=inp.long_press_ms,
            bounce_ms=inp.bounce_ms,
        )
        self._press_queue: list[Press] = []
        state.press_listeners.append(self._on_press_edge)
        state.toggle_listeners.append(lambda t: self._press_queue.append(Press("silent", t)))
        state.action_listeners.append(lambda k, t: self._press_queue.append(Press(k, t)))
        # Live overrides owned by the backend and pushed to every client.
        self.mindset_override: str | None = None
        self.budget_live: int | None = None  # driver-menu calls-per-lap override
        pages = store.current().ui.pages
        self.page = pages[0] if pages else "race"
        self._page_manual_t: float | None = None
        # Driver -> pit wall menu (docs/12); opinions bias advice for a few laps.
        self.menu = DriverMenu()
        self._menu_replies = ReplyPicker()
        self._menu_signal_names: dict[str, frozenset[str]] = {}
        self._menu_signal_hash = self.store.hash
        self.opinions: dict[str, tuple[str, int]] = {}  # topic -> (item id, lap)
        self._apply_mode()
        # Crash recovery (docs/18): heartbeat to SQLite; on restart replay the
        # recording tail with the rules muted.
        self.recording_path_source: Any = None  # callable -> Path | None
        self._last_heartbeat: float | None = None
        self.recovering = False
        self.alive_path: Path | None = None  # touched each live loop for the supervisor
        self._alive_t = -math.inf
        self.tick_errors = 0
        self.db: Any = dispatcher.log.db
        self.dispatcher.tuned_cooldown = load_cooldown_mults(self.db)
        self._laps_written = 0
        self._session_upserted: int | None = None
        self._parc_ferme_written: int | None = None
        self._weekend_structure_written: tuple[int, ...] = ()
        self.session_origin_started_at: float | None = None
        self._track_loaded: int | None = None
        self._session_ended_written = False
        # M3 model outputs, exposed on the engine until H2 moves them into
        # the snapshot.
        self.deg_fit: DegFit | None = None
        self.pit_loss_prior: Prior | None = None
        self.pit_plan: PitPlan = NO_PLAN
        self._fresh_prior: DegFit | None = None
        self._green_pit_loss_s = 0.0
        self._sc_pit_loss_s = 0.0
        # Named strategy plans (docs/03 "Strategy plans").
        self.plan_tracker = PlanTracker()
        self._plan_key: tuple[int, int, bool, bool, int, bool] | None = None
        self._stops_done = 0
        self._used_compounds: set[int] = set()
        self._prev_race_phase = ""
        self._compound_priors: dict[int, DegFit] = {}
        # Battle state + learned pass model (docs/18 Battle state).
        self.battle_tracker = BattleTracker()
        self._battle_rates: tuple[int, BattleRates] | None = None
        self.fuel_budget: FuelBudget | None = None
        self.energy_budget: EnergyBudget | None = None
        self.energy_prev_lap: EnergyBudget | None = None
        self._energy_lap_tracker = _EnergyLapTracker()
        self._pit_in_lap: LapSummary | None = None
        self._pending_pit_loss: _PendingPitLoss | None = None
        self._folded_stints: set[tuple[int, int]] = set()
        self._weekend_prior_cache: dict[tuple[int, int], tuple[float, int]] = {}
        self._fuel_last_kg: float | None = None
        self._session_fuel_last_kg: float | None = None
        self._session_fuel_deltas: list[float] = []
        self._stint_fuel_ref = 0.0
        self._stint_laps: list[LapRow] = []
        self._model_key: tuple[int | None, int, int] | None = None
        self._fuel_prior = Prior(0.0, 0.0, "default")
        self._wear_per_lap = 0.0
        self._stint_wear0: tuple[float, ...] = ()
        self._stint_age0 = 0.0
        self._stint_compound0 = -1

        def _on_rewind(t: float) -> None:
            dispatcher.purge(reason="flashback", now=t)
            self._reset_energy_lap_tracking()

        state.rewind_listeners.append(_on_rewind)
        state.session_listeners.append(self._on_new_session)
        state.session_end_listeners.append(self.fold_open_stint)

    @property
    def tick_period(self) -> float:
        return 1.0 / self.store.current().engine.tick_hz

    def _on_press_edge(self, t: float, down: bool) -> None:
        press = self.detector.edge(t, down)
        if press is not None:
            self._press_queue.append(press)

    # -- live overrides: mindset (UDP Action 2) and dashboard page (Action 4) --

    @property
    def mindset(self) -> str:
        return self.mindset_override or self.store.current().mindset.active

    def mode(self) -> dict[str, Any]:
        settings = self.store.current()
        return resolve_mindset(settings.mindsets, self.mindset)

    def _apply_mode(self) -> None:
        mode = self.mode()
        if self.rule_engine is not None:
            self.rule_engine.mode = mode
        budget = (
            self.budget_live if self.budget_live is not None else mode.get("call_budget_per_lap")
        )
        self.dispatcher.budget_override = int(budget) if budget is not None else None
        self.dispatcher.log.mindset = self.mindset

    def set_mindset(self, name: str, now: float) -> bool:
        if name not in self.store.current().mindsets or name == self.mindset:
            return False
        self.mindset_override = name
        self._apply_mode()
        snap = self.dispatcher.latest_snapshot or self.state.snapshot(now)
        self.dispatcher.announce_mindset(name, dataclasses.replace(snap, now=now))
        return True

    def cycle_budget(self) -> int:
        """Next driver-menu calls-per-lap step; overrides the mindset's budget."""
        steps = self.store.current().menu.budget_steps or [4]
        cur = self.dispatcher.budget_override
        nxt = next((s for s in steps if cur is None or s > cur), steps[0])
        self.budget_live = nxt
        self._apply_mode()
        return nxt

    def cycle_mindset(self, now: float) -> None:
        cycle = [m for m in self.store.current().input.mindset_cycle if m]
        if not cycle:
            return
        cur = self.mindset
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
        self.set_mindset(nxt, now)

    def set_page(self, name: str, now: float, *, manual: bool = True) -> bool:
        if name not in self.store.current().ui.pages:
            return False
        if manual:
            self._page_manual_t = now
        if name == self.page:
            return False
        self.page = name
        self.dispatcher.log.write(
            {"t": now, "outcome": "page", "rule_id": None, "text": name, "manual": manual}
        )
        return True

    def cycle_page(self, now: float) -> None:
        pages = self.store.current().ui.pages
        if not pages:
            return
        i = pages.index(self.page) if self.page in pages else -1
        self.set_page(pages[(i + 1) % len(pages)], now)

    def _auto_page(self, snap: Snapshot, now: float) -> None:
        """Contextual page (off by default). A manual choice holds, and a
        page never changes under a fresh call banner."""
        ui = self.store.current().ui
        if not ui.auto_page:
            return
        if (
            self._page_manual_t is not None
            and now - self._page_manual_t < ui.auto_page_manual_hold_s
        ):
            return
        last = self.dispatcher.last_call_t
        if last is not None and now - last < ui.auto_page_call_hold_s:
            return
        gap = ui.auto_page_battle_gap_s
        if snap.race_phase in ("formation", "sc", "vsc"):
            target = "track"
        elif snap.session_kind == "race" and (
            (snap.rival_ahead_idx >= 0 and 0 < snap.gap_ahead_s <= gap)
            or (snap.rival_behind_idx >= 0 and 0 < snap.gap_behind_s <= gap)
        ):
            target = "race"
        else:
            target = ui.pages[0] if ui.pages else "race"
        self.set_page(target, now, manual=False)

    def client_message(self, msg: dict[str, Any]) -> None:
        """Dashboard control messages: {"type":"mindset","name"},
        {"type":"page","name"|"cycle":true} and
        {"type":"menu","op":"up"|"down"|"confirm"|"close"}."""
        now = self.clock.now()
        if msg.get("type") == "mindset":
            name = msg.get("name")
            if isinstance(name, str):
                self.set_mindset(name, now)
            else:
                self.cycle_mindset(now)
        elif msg.get("type") == "page":
            name = msg.get("name")
            if isinstance(name, str):
                self.set_page(name, now)
            else:
                self.cycle_page(now)
        elif msg.get("type") == "menu":
            op = msg.get("op")
            kind = _MENU_CLIENT_OPS.get(op) if isinstance(op, str) else None
            if kind is not None:
                self._press_queue.append(Press(kind, now))

    def client_press(self, down: bool) -> None:
        """WebSocket/spacebar press path (docs/12): feed the same detector."""
        self._on_press_edge(self.clock.now(), down)

    # -- driver menu (docs/12) -------------------------------------------------

    def menu_payload(self, now: float) -> dict[str, object]:
        return self.menu.payload(self.store.current().menu, now)

    def _menu_press(self, p: Press, snapshot: Snapshot) -> bool:
        """Route a press through the menu. False: not a menu press; the
        caller handles it as usual."""
        settings = self.store.current().menu
        kind = p.kind
        if kind.startswith("item:"):
            item = next((i for i in settings.items if i.id == kind[5:]), None)
            if item is not None:
                self._menu_close(p.t, snapshot, "shortcut")
                self._menu_answer(item, p.t, snapshot, "shortcut")
            return True
        if self.menu.open and kind in ("page", "mindset"):
            remap = self.store.current().input.menu_open_actions
            op = remap.page if kind == "page" else remap.mindset
            if op:
                kind = f"menu_{op}"
        if kind in ("menu_up", "menu_down"):
            was_open = self.menu.open
            item = self.menu.step(
                settings, -1 if kind == "menu_up" else 1, p.t, self._menu_ns(snapshot)
            )
            if item is None:
                return True
            if not was_open:
                self._menu_log(p.t, snapshot, "menu_open", None, "")
            if settings.speak_on_scroll:
                self.dispatcher.menu_prompt(item.label, snapshot)
            return True
        if kind == "menu_close":
            self._menu_close(p.t, snapshot, "close")
            return True
        if not self.menu.open:
            return kind == "menu_confirm"
        if kind in ("ack", "menu_confirm"):
            self._menu_confirm(p.t, snapshot)
            return True
        if kind in ("neg", "bookmark"):
            self._menu_close(p.t, snapshot, "cancel")
            return True
        return False

    def _plan_thresholds(self, snap: Snapshot) -> Mapping[str, object]:
        th = self.store.current().thresholds
        if not snap.is_sprint:
            return th
        return {**th, "plan_two_compound_rule": 0}  # no mandatory stop in a sprint

    def _menu_ns(self, snapshot: Snapshot) -> dict[str, Any]:
        cfg = self.store.current()
        limits = cfg.engine.staleness_s
        ns = namespace_data(
            snapshot,
            thresholds=cfg.thresholds,
            mode=cfg.resolved_mindset(),
            staleness_age=snapshot.age,
            staleness_limit=lambda n: limits.get(n, STALENESS_DEFAULT_S),
        )
        return ns

    def _menu_close(self, t: float, snapshot: Snapshot, reason: str) -> None:
        if not self.menu.open:
            return
        self.menu.close()
        self.dispatcher.cancel_menu_prompt()
        self._menu_log(t, snapshot, "menu_close", None, reason)

    def _menu_log(
        self,
        t: float,
        snapshot: Snapshot,
        outcome: str,
        item: MenuItemModel | None,
        text: str,
        inputs: dict[str, Any] | None = None,
    ) -> None:
        self.dispatcher.log.write(
            {
                "t": t,
                "session_time": snapshot.session_time,
                "lap": snapshot.lap_num,
                "lap_distance": snapshot.lap_distance,
                "call_id": None,
                "rule_id": f"menu:{item.id}" if item else None,
                "priority": 1 if item else None,
                "outcome": outcome,
                "suppressed_by": None,
                "item_id": item.id if item else None,
                "kind": item.kind if item else None,
                "topic": item.topic if item else None,
                "label": item.label if item else None,
                "inputs": inputs or {},
                "text": text,
            }
        )

    def _menu_confirm(self, t: float, snapshot: Snapshot) -> None:
        item = self.menu.selected(self.store.current().menu)
        self.menu.close()
        self.dispatcher.cancel_menu_prompt()
        if item is not None:
            self._menu_answer(item, t, snapshot, "menu")

    def _menu_signal_values(
        self, item: MenuItemModel, snapshot: Snapshot
    ) -> dict[str, int | float]:
        current_hash = self.store.hash
        if self._menu_signal_hash != current_hash:
            self._menu_signal_names.clear()
            self._menu_signal_hash = current_hash
        names = self._menu_signal_names.get(item.id)
        if names is None:
            rules = {rule.id: rule for rule in self.store.current().rules}
            names = frozenset(
                name
                for rule_id in item.related_rules
                if (rule := rules.get(rule_id)) is not None
                for name in expr_names(rule.when)
            )
            self._menu_signal_names[item.id] = names
        signals: dict[str, int | float] = {}
        for name in names:
            if not hasattr(snapshot, name):
                continue
            value = getattr(snapshot, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            signals[name] = round(value, 3)
        return signals

    def _menu_answer(self, item: MenuItemModel, t: float, snapshot: Snapshot, via: str) -> None:
        snap = dataclasses.replace(snapshot, now=t)
        case, values = answer(item, snap, self.mindset)
        if item.action == "budget":
            values["budget"] = str(self.cycle_budget())
        text = self._menu_replies.pick(item, case, values)
        inputs: dict[str, Any] = {"case": case, "via": via, **values}
        if item.kind == "question" and item.related_rules:
            inputs["signals"] = self._menu_signal_values(item, snap)
        self._menu_log(t, snap, "driver_input", item, text, inputs)
        if item.kind == "opinion" and item.topic:
            self.opinions[item.topic] = (item.id, snapshot.lap_num)
        if item.action == "mindset":
            self.cycle_mindset(t)
        elif item.action == "silent":
            self.dispatcher.toggle_silent(snap)
        elif item.action == "page":
            self.cycle_page(t)
        if text:
            self.dispatcher.menu_reply(text, f"menu:{item.id}", snap)

    def driver_balance(self, lap: int) -> str:
        """Latest balance opinion while it still holds (docs/12 advice bias)."""
        held = self.opinions.get("balance")
        hold = self.store.current().menu.opinion_hold_laps
        if held is None or lap - held[1] > hold:
            return ""
        return held[0]

    def _on_new_session(self, uid: int) -> None:
        pending = self._pending_pit_loss
        if pending is not None:
            if self.db is None:
                self._pending_pit_loss = None
            else:
                try:
                    laps = self.db.laps_for(pending.session_uid, 0)
                    self._flush_pending_pit_loss(pending.session_uid, laps, force=True)
                except sqlite3.Error:
                    self._pending_pit_loss = None
        self.dispatcher.reset_session()
        self._reset_energy_lap_tracking()
        self.menu.close()
        self._menu_replies.reset()
        self.opinions.clear()
        self._laps_written = len(self.state.laps)
        self._session_ended_written = False
        self._pit_in_lap = None
        self._folded_stints.clear()
        self._weekend_prior_cache.clear()
        self._fuel_last_kg = None
        self._session_fuel_last_kg = None
        self._session_fuel_deltas = []
        self.deg_fit = None
        self.plan_tracker.reset()
        self._plan_key = None
        self.battle_tracker.reset()
        self._battle_rates = None
        self._stops_done = 0
        self._stint_wear0 = ()
        self._stint_compound0 = -1
        self._used_compounds.clear()
        self._prev_race_phase = ""
        self.session_origin_started_at = None
        self._parc_ferme_written = None
        self._weekend_structure_written = ()
        self.setup_advisor.reset(uid)

    def _evaluate_live_setup(self, uid: int) -> None:
        self.setup_advisor.evaluate_live(self.db, uid, self.store.current())

    def _store_debrief_setup(self, uid: int) -> None:
        if self.db is None:
            return
        self.setup_advisor.store_debrief(self.db, uid, self.store.current())

    def _update_setup_stop_wing(self) -> None:
        self.setup_advisor.update_stop_wing(self.pit_plan.plan in _STOP_PLANS)

    def _synthetic_session(self, uid: int | None = None) -> bool:
        session_uid = self.state.session_uid if uid is None else uid
        return self.synthetic_source or (session_uid is not None and is_synthetic_uid(session_uid))

    def _reset_energy_lap_tracking(self) -> None:
        self._energy_lap_tracker.reset()
        self.energy_prev_lap = None

    def _upsert_session(self, uid: int) -> None:
        if self.db is None:
            return
        self._session_upserted = uid
        snap = self.state.snapshot(self.clock.now())
        self.db.upsert_session(
            uid,
            track_id=snap.track_id,
            session_type=snap.session_type,
            started_at=(
                self.session_origin_started_at
                if self.session_origin_started_at is not None
                else time.time()
            ),
            config_hash=self.store.hash,
            weather=snap.weather,
            game_mode=snap.game_mode,
            weekend_link=snap.weekend_link,
            weekend_structure=snap.weekend_structure,
            calls_mode=(
                "off"
                if self.store.current().policy.quiet or not self.store.current().speech.enabled
                else "on"
            ),
            parc_ferme=snap.parc_ferme if snap.parc_ferme >= 0 else None,
            synthetic=self._synthetic_session(uid),
        )
        self._parc_ferme_written = snap.parc_ferme if snap.parc_ferme >= 0 else None
        self._weekend_structure_written = tuple(snap.weekend_structure)

    def _write_laps(self) -> None:
        if self.db is None or self.state.session_uid is None:
            return
        if self.recovering:
            # Rebuilt laps are already committed; don't insert or fold twice.
            self.state.rival_laps.clear()
            self.state.setup_changes.clear()
            self._laps_written = len(self.state.laps)
            return
        uid = self.state.session_uid
        parc_ferme = self.state.parc_ferme_rules
        if parc_ferme >= 0 and parc_ferme != self._parc_ferme_written:
            self.db.set_session_parc_ferme(uid, parc_ferme)
            self._parc_ferme_written = parc_ferme
        structure = tuple(self.state.weekend_structure)
        if structure and structure != self._weekend_structure_written:
            self.db.set_weekend_structure(uid, structure)
            self._weekend_structure_written = structure
        if self.state.setup_rewind_t is not None:
            self.db.delete_setup_changes_after(uid, self.state.setup_rewind_t)
            self.state.setup_rewind_t = None
        setup_changed = bool(self.state.setup_changes)
        for change in self.state.setup_changes:
            to_id = self.db.setup_state_id(change.to_hash, change.fields)
            from_id = self.db.setup_state_id(change.from_hash) if change.from_hash else None
            self.db.insert_setup_change(
                uid,
                change.lap_num,
                change.session_time,
                from_id,
                to_id,
            )
        self.state.setup_changes.clear()
        for car_idx, lap in self.state.rival_laps:
            self.db.insert_lap(uid, car_idx, lap)
        self.state.rival_laps.clear()
        new_laps = self.state.laps[self._laps_written :]
        for lap in new_laps:
            self.db.insert_lap(
                uid,
                0,
                lap,
                setup_state_id=self.db.setup_state_id(lap.setup_hash) if lap.setup_hash else None,
            )
        self._laps_written = len(self.state.laps)
        if new_laps and self.state.total_laps > 0:
            self.db.set_session_total_laps(uid, self.state.total_laps)
        for lap in new_laps:
            self._on_player_lap(uid, lap)
        if new_laps or setup_changed:
            if uid != self.setup_advisor.session_uid:
                self.setup_advisor.reset(uid)
            if new_laps or session_kind(self.state.session_type) == "race":
                self._evaluate_live_setup(uid)
            else:
                # Garage advice was built on the setup the driver just changed.
                # Drop it until a lap on the new setup exists.
                self.setup_advisor.clear()

    def _on_player_lap(self, uid: int, lap: LapSummary) -> None:
        """M3 persistence hooks (docs/18): refit the stint, measure pit loss
        when an out-lap completes, fold fuel-burn deltas."""
        self._note_session_fuel(lap)
        db = self.db
        if db is None:
            return
        settings = self.store.current()
        th = settings.thresholds
        track_id = self.state.track_id
        all_laps = db.laps_for(uid, 0)

        # Current stint = contiguous tail with one compound and strictly
        # increasing tyre_age. Whatever precedes the tail is a closed stint.
        tail: list[LapRow] = []
        for row in reversed(all_laps):
            if tail and (
                row.compound != tail[-1].compound or row.tyre_age_laps >= tail[-1].tyre_age_laps
            ):
                break
            tail.append(row)
        tail.reverse()

        if len(tail) < len(all_laps):
            # A stint just closed; fold its fitted params exactly once.
            prev = self._stint_rows(all_laps[: len(all_laps) - len(tail)])
            key = (uid, prev[0].lap_num) if prev else (uid, tail[0].lap_num)
            if key not in self._folded_stints:
                self._folded_stints.add(key)
                if prev:
                    prior = self._deg_prior(track_id, prev[0].compound, settings)
                    fit = fit_stint(
                        prev,
                        prior,
                        min_laps=int(self._th("deg_min_laps", 3)),
                        fuel_coeff_fixed=None,
                        deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                        deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
                    )
                    self._fold_fit(track_id, prev[0].compound, fit)
                    self._weekend_prior_cache.pop((uid, prev[0].compound), None)

        if tail:
            self._stint_laps = tail
            self._stint_fuel_ref = max((r.fuel_kg for r in tail), default=0.0)
            compound = tail[0].compound
            prior = self._deg_prior(track_id, compound, settings)
            fuel_name = self._learned_name(track_id, compound, "fuel_ms_per_lap")
            fuel_param = db.get_param(track_id, compound, fuel_name)
            fuel_fixed = (
                fuel_param.value
                if fuel_param is not None and fuel_param.weight >= self._th("prior_min_weight", 2)
                else None
            )
            fit = fit_stint(
                tail,
                prior,
                min_laps=int(self._th("deg_min_laps", 3)),
                fuel_coeff_fixed=fuel_fixed,
                deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
            )
            self.deg_fit = fit
            db.upsert_stint(
                uid,
                0,
                compound,
                tail[0].lap_num,
                tail[-1].lap_num,
                fit,
                setup_state_id=majority_state(tail),
            )

        # Pit loss: the lane-entry lap is the in-lap, even when the box is after the line.
        if (
            "after_in_lap" in lap.invalid_reasons
            and self._pit_in_lap is not None
            and self._pit_in_lap.lap_num == lap.lap_num - 1
        ):
            in_lap = self._pit_in_lap
            self._pit_in_lap = None
            skip = {"flashback", "red_flag"}
            reasons = set(in_lap.invalid_reasons) | set(lap.invalid_reasons)
            if not reasons & skip:
                ref = ref_pace_ms(all_laps, in_lap.lap_num)
                if ref > 0:
                    in_row = next(r for r in all_laps if r.lap_num == in_lap.lap_num)
                    out_row = next(r for r in all_laps if r.lap_num == lap.lap_num)
                    neutralised = max(in_row.sc_status, out_row.sc_status)
                    self._pending_pit_loss = _PendingPitLoss(
                        session_uid=uid,
                        track_id=track_id,
                        in_lap=in_row,
                        out_lap=out_row,
                        ref_pace_ms=ref,
                        lane_ms=self.state.pit_lane_time_ms,
                        neutralised=neutralised,
                    )
        elif "pitted" in lap.invalid_reasons:
            pending = self._pending_pit_loss
            if pending is not None and pending.out_lap.lap_num == lap.lap_num - 1:
                self._pending_pit_loss = None
            elif pending is not None:
                self._flush_pending_pit_loss(uid, all_laps, force=True)
            self._pit_in_lap = lap
        else:
            self._flush_pending_pit_loss(uid, all_laps)

        # Fuel burn per lap: fold each consecutive-valid-lap delta.
        if lap.valid and lap.fuel_kg > 0:
            if self._fuel_last_kg is not None:
                delta = self._fuel_last_kg - lap.fuel_kg
                if (
                    track_id >= 0
                    and not self._synthetic_session(uid)
                    and self._th("fuel_delta_min_kg", 0) < delta < self._th("fuel_delta_max_kg", 10)
                ):
                    db.fold_param(
                        track_id,
                        0,
                        "fuel_kg_per_lap",
                        delta,
                        param_weight_cap=self._th("param_weight_cap", 50),
                    )
            self._fuel_last_kg = lap.fuel_kg
        elif lap.fuel_kg > 0:
            self._fuel_last_kg = lap.fuel_kg

        self.pit_loss_prior = current_pit_loss(
            db,
            uid,
            track_id,
            lap.sc_status,
            settings.track,
            th,
        )

    def _flush_pending_pit_loss(
        self,
        uid: int,
        laps: list[LapRow],
        *,
        force: bool = False,
    ) -> None:
        pending = self._pending_pit_loss
        if pending is None:
            return
        if pending.session_uid != uid:
            self._pending_pit_loss = None
            return
        if self.db is None:
            self._pending_pit_loss = None
            return
        ref_laps = max(1, int(self._th("pit_ref_after_laps", 3)))
        after_laps = [
            row
            for row in laps
            if row.lap_num > pending.out_lap.lap_num and row.valid == 1 and row.lap_time_ms > 0
        ]
        if not force and len(after_laps) < ref_laps:
            return
        ref_after = ref_pace_after_ms(laps, pending.out_lap.lap_num, ref_laps) or None
        self._pending_pit_loss = None
        pit = measure(
            pending.in_lap,
            pending.out_lap,
            pending.lane_ms,
            pending.ref_pace_ms,
            pending.neutralised,
            ref_after_ms=ref_after,
        )
        if not (
            self._th("pit_loss_min_ms", 5_000) <= pit.loss_ms <= self._th("pit_loss_max_ms", 60_000)
        ):
            return
        self.db.insert_pit_event(
            pending.session_uid,
            0,
            pending.in_lap.lap_num,
            pit.loss_ms,
            pending.neutralised,
            pit.lane_ms,
            pit.in_lap_ms,
            pit.out_lap_ms,
            pit.ref_pace_ms,
        )
        suffix = {0: "green", 1: "sc", 2: "vsc"}.get(pending.neutralised, "green")
        if pending.track_id >= 0 and not self._synthetic_session(pending.session_uid):
            self.db.fold_param(
                pending.track_id,
                0,
                f"pit_loss_{suffix}_ms",
                float(pit.loss_ms),
                param_weight_cap=self._th("param_weight_cap", 50),
            )

    def _note_session_fuel(self, lap: LapSummary) -> None:
        """Burn per lap measured this session; pit, SC and flashback laps break the chain."""
        if lap.fuel_kg <= 0:
            return
        last = self._session_fuel_last_kg
        green = lap.sc_status == 0 and not set(lap.invalid_reasons) & _NON_GREEN_REASONS
        if green and last is not None and 0.0 < last - lap.fuel_kg < 10.0:
            self._session_fuel_deltas.append(last - lap.fuel_kg)
        self._session_fuel_last_kg = lap.fuel_kg if green else None

    def _fuel_per_lap(self, laps_remaining: int) -> tuple[float, str]:
        """This session's measured burn, else the game's own MFD estimate, else the prior."""
        deltas = self._session_fuel_deltas
        if len(deltas) >= int(self._th("fuel_session_min_laps", 2)):
            return median(deltas[-8:]), "session"
        state = self.state
        laps_in_tank = laps_remaining + state.fuel_remaining_laps
        if laps_remaining > 0 and state.fuel_in_tank > 0 and laps_in_tank > 0.5:
            per_lap = state.fuel_in_tank / laps_in_tank
            if 0.2 <= per_lap <= 5.0:
                return per_lap, "mfd"
        return self._fuel_prior.value, self._fuel_prior.source

    def _stint_rows(self, laps: list[LapRow]) -> list[LapRow]:
        """Trailing contiguous stint (same compound, increasing tyre age)."""
        tail: list[LapRow] = []
        for row in reversed(laps):
            if tail and (
                row.compound != tail[-1].compound or row.tyre_age_laps >= tail[-1].tyre_age_laps
            ):
                break
            tail.append(row)
        tail.reverse()
        return tail

    def _deg_prior(self, track_id: int, compound: int, settings: Any) -> DegFit:
        overlay = settings.track
        min_w = self._th("prior_min_weight", 2)
        uid = self.state.session_uid
        fuel_p = resolve_prior(
            self.db,
            track_id,
            compound,
            self._learned_name(track_id, compound, "fuel_ms_per_lap"),
            overlay_value=None,
            default=self._th("fuel_ms_per_lap_default", 30),
            min_weight=min_w,
        )
        deg_p: Prior | None = None
        if self.db is not None and uid is not None:
            cache_key = (uid, compound)
            weekend = self._weekend_prior_cache.get(cache_key)
            if weekend is None:
                rmse_bad = self._th("deg_rmse_bad_ms", 800)
                stints = [
                    stint
                    for stint in self.db.weekend_stints(uid, track_id, compound)
                    if stint.rmse_ms <= rmse_bad
                ]
                n = sum(stint.n_valid_laps for stint in stints)
                if n:
                    value = (
                        sum(
                            fuel_adjusted_deg(
                                stint.deg_ms_per_lap, stint.fuel_ms_per_lap, fuel_p.value
                            )
                            * stint.n_valid_laps
                            for stint in stints
                        )
                        / n
                    )
                else:
                    value = 0.0
                weekend = (value, n)
                self._weekend_prior_cache[cache_key] = weekend
            if weekend[1] >= self._th("weekend_min_laps", 6):
                deg_p = Prior(weekend[0], float(weekend[1]), "weekend")
        if deg_p is None:
            deg_p = resolve_prior(
                self.db,
                track_id,
                compound,
                self._learned_name(track_id, compound, "deg_ms_per_lap"),
                overlay_value=(
                    overlay.deg_ms_per_lap.get(compound) if overlay is not None else None
                ),
                default=self._th("deg_default_ms_per_lap", 80),
                min_weight=min_w,
            )
        base_p = resolve_prior(
            self.db,
            track_id,
            compound,
            self._learned_name(track_id, compound, "base_ms"),
            overlay_value=(
                float(overlay.base_pace_ms)
                if overlay is not None and overlay.base_pace_ms > 0
                else None
            ),
            default=self._th("release_fallback_lap_s", 95.0) * 1000.0,
            min_weight=min_w,
        )
        deg = deg_p.value
        if deg_p.source == "learned" and self.db is not None:
            deg_name = self._learned_name(track_id, compound, "deg_ms_per_lap")
            suffix = deg_name.removeprefix("deg_ms_per_lap")
            ref = self.db.get_param(track_id, compound, DEG_FUEL_REF + suffix)
            deg = fuel_adjusted_deg(
                deg, ref.value if ref is not None and ref.weight >= min_w else None, fuel_p.value
            )
        confidence = {"weekend": 0.45, "learned": 0.5, "overlay": 0.35}.get(deg_p.source, 0.2)
        if deg_p.source == "learned":
            # A thin learned prior (few folded laps) is pulled toward the
            # overlay/default so one steep stint cannot set the plan alone.
            full = self._th("prior_full_weight", 12)
            trust = min(deg_p.weight / full, 1.0) if full > 0 else 1.0
            fallback = overlay.deg_ms_per_lap.get(compound) if overlay is not None else None
            if fallback is None:
                fallback = self._th("deg_default_ms_per_lap", 80)
            deg = trust * deg + (1.0 - trust) * float(fallback)
            confidence = 0.2 + (confidence - 0.2) * trust
        if base_p.source == "default" and self.db is not None and uid is not None:
            seeded = session_base_ms(self.db.laps_for(uid, 0), deg)
            if seeded > 0:
                base_p = Prior(seeded, 0.0, "session")
        return DegFit(
            base_ms=base_p.value,
            deg_ms_per_lap=deg,
            fuel_ms_per_lap=fuel_p.value,
            n=0,
            rmse_ms=0.0,
            confidence=confidence,
            source=deg_p.source,
        )

    def fold_open_stint(self) -> None:
        """Persist the current tail stint when a session closes."""
        if self.db is None or self.state.session_uid is None:
            self._pending_pit_loss = None
            return
        self._write_laps()
        uid = self.state.session_uid
        rows = self.db.laps_for(uid, 0)
        self._flush_pending_pit_loss(uid, rows, force=True)
        stint = self._stint_rows(rows)
        if not stint:
            return
        key = (uid, stint[0].lap_num)
        if key in self._folded_stints:
            return
        self._folded_stints.add(key)
        settings = self.store.current()
        prior = self._deg_prior(self.state.track_id, stint[0].compound, settings)
        fit = fit_stint(
            stint,
            prior,
            min_laps=int(self._th("deg_min_laps", 3)),
            fuel_coeff_fixed=None,
            deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
            deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
        )
        self.db.upsert_stint(
            uid,
            0,
            stint[0].compound,
            stint[0].lap_num,
            stint[-1].lap_num,
            fit,
            setup_state_id=majority_state(stint),
        )
        self._weekend_prior_cache.pop((uid, stint[0].compound), None)
        self._fold_fit(self.state.track_id, stint[0].compound, fit)

    def _fold_fit(self, track_id: int, compound: int, fit: DegFit) -> None:
        """Fold a stint fit into the learned priors when it is a clean fit on
        an identified track; noisy stints stay in `stints` only."""
        if (
            self.db is None
            or track_id < 0
            or self._synthetic_session()
            or not fit_is_clean(
                fit,
                deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
                base_min_ms=self._th("learn_base_min_ms", 40_000),
                base_max_ms=self._th("learn_base_max_ms", 200_000),
            )
        ):
            return
        race_laps = self._race_laps()
        params = [
            ("deg_ms_per_lap", fit.deg_ms_per_lap),
            (DEG_FUEL_REF, fit.fuel_ms_per_lap),
            ("base_ms", fit.base_ms),
        ]
        if fit.fuel_fitted:
            params.append(("fuel_ms_per_lap", fit.fuel_ms_per_lap))
        for name, value in params:
            self.db.fold_param(
                track_id,
                compound,
                scoped(name, race_laps),
                value,
                weight=float(fit.n),
                param_weight_cap=self._th("param_weight_cap", 50),
            )

    def _learned_name(self, track_id: int, compound: int, name: str) -> str:
        """The race-distance prior once it has weight, else the practice one."""
        name_at = scoped(name, self._race_laps())
        if name_at == name or self.db is None:
            return name
        p = self.db.get_param(track_id, compound, name_at)
        return name_at if p is not None and p.weight >= self._th("prior_min_weight", 2) else name

    def _race_laps(self) -> int:
        """Race distance the stint-derived priors are scoped to; 0 outside races."""
        kind = session_kind(self.state.session_type)
        return self.state.total_laps if kind == "race" and self.state.total_laps > 0 else 0

    def _th(self, name: str, default: float) -> float:
        return threshold(self.store.current().thresholds, name, default)

    def _lap_fraction(self) -> float:
        state = self.state
        if state.track_length_m <= 0:
            return 0.0
        return min(max(state.lap_distance / state.track_length_m, 0.0), 1.0)

    def _update_model(self) -> None:
        """Refresh priors (once per lap) and budgets (every tick), then hand
        the whole model view to SessionState for the rules-facing snapshot."""
        state = self.state
        settings = self.store.current()
        mode = self.mode()
        state.set_mode_offsets(
            thermal_warn_offset_c=float(mode.get("thermal_warn_offset_c", 0) or 0)
        )
        track_id = state.track_id
        overlay = settings.track
        key = (state.session_uid, self._laps_written, state.lap_num)
        if key != self._model_key:
            self._model_key = key
            self.pit_loss_prior = current_pit_loss(
                self.db,
                state.session_uid,
                track_id,
                state.safety_car_status,
                overlay,
                settings.thresholds,
            )
            self._fuel_prior = resolve_prior(
                self.db,
                track_id,
                0,
                "fuel_kg_per_lap",
                overlay_value=(
                    overlay.fuel_kg_per_lap
                    if overlay is not None and overlay.fuel_kg_per_lap > 0
                    else None
                ),
                default=self._th("fuel_kg_per_lap_default", 1.7),
                min_weight=self._th("prior_min_weight", 2),
            )
            green = current_pit_loss(
                self.db, state.session_uid, track_id, 0, overlay, settings.thresholds
            )
            self._green_pit_loss_s = green.value / 1000.0
            sc = current_pit_loss(
                self.db, state.session_uid, track_id, 1, overlay, settings.thresholds
            )
            self._sc_pit_loss_s = sc.value / 1000.0
            self._compound_priors.clear()
            self._fresh_prior = self._deg_prior(track_id, state.tyre_compound, settings)
            # Wear rate this stint: median of positive consecutive deltas.
            wear_deltas = [
                b.wear_pct - a.wear_pct
                for a, b in zip(self._stint_laps, self._stint_laps[1:], strict=False)
                if a.wear_pct > 0 and b.wear_pct > a.wear_pct
            ]
            self._wear_per_lap = (
                median(wear_deltas) if wear_deltas else self._th("wear_per_lap_default_pct", 2.5)
            )

        laps_remaining = max(0, state.total_laps - state.lap_num + 1) if state.total_laps > 0 else 0
        lap_num = state.lap_num
        deployed_j = state.ers_deployed_this_lap_j
        harvested_j = state.ers_harvested_mguk_j + state.ers_harvested_mguh_j
        energy_capacity_j = self._th("ers_store_capacity_j", 4_000_000)
        energy_floor_pct = float(mode.get("ers_soc_floor_pct", 0) or 0)
        energy_over_tolerance_j = self._th("energy_over_tolerance_j", 400_000)
        energy_attack_ok = mode.get("ers_policy") == "attack_rival"
        (
            live_deployed_j,
            live_harvested_j,
            allowance_store_j,
            live_laps_remaining,
        ) = self._energy_lap_tracker.observe(
            lap_num,
            laps_remaining,
            state.ers_store_energy_j,
            deployed_j,
            harvested_j,
            counters_current=state.ers_counters_current,
            finished=state.ers_finished_lap,
            store_capacity_j=energy_capacity_j,
            soc_floor_pct=energy_floor_pct,
            over_tolerance_j=energy_over_tolerance_j,
            attack_ok=energy_attack_ok,
        )
        self.energy_prev_lap = self._energy_lap_tracker.prev_lap
        per_lap_kg, fuel_source = self._fuel_per_lap(laps_remaining)
        self.fuel_budget = fuel_budget(
            laps_remaining=laps_remaining,
            fuel_in_tank_kg=state.fuel_in_tank,
            per_lap_kg=per_lap_kg,
            source=fuel_source,
            lap_done=(
                state.lap_distance / state.track_length_m if state.track_length_m > 0 else 0.0
            ),
        )
        self.energy_budget = energy_budget(
            store_j=state.ers_store_energy_j,
            store_capacity_j=energy_capacity_j,
            laps_remaining=live_laps_remaining,
            deployed_this_lap_j=live_deployed_j,
            harvested_this_lap_j=live_harvested_j,
            allowance_store_j=allowance_store_j,
            soc_floor_pct=energy_floor_pct,
            over_tolerance_j=energy_over_tolerance_j,
            attack_ok=energy_attack_ok,
        )

        fit = self.deg_fit
        wear = state.tyres_wear.as_tuple()
        age = state.tyre_age_laps
        if (
            not self._stint_wear0
            or state.tyre_compound != self._stint_compound0
            or age < self._stint_age0
            or any(w < w0 - 1.0 for w, w0 in zip(wear, self._stint_wear0, strict=True))
        ):
            self._stint_wear0 = wear
            self._stint_age0 = age + self._lap_fraction()
            self._stint_compound0 = state.tyre_compound
        lop = corner_wear_life(
            wear,
            self._stint_wear0,
            age + self._lap_fraction() - self._stint_age0,
            wear_cliff_pct=self._th("wear_cliff_pct", 70),
            default_rate_pct=self._wear_per_lap,
        )
        predicted = 0
        if fit is not None:
            lop = min(
                lop,
                laps_of_pace(
                    planning_fit(fit, self._fresh_prior, self._th("deg_rmse_bad_ms", 800))
                    if self._fresh_prior is not None
                    else fit,
                    age,
                    max(wear),
                    cliff_ms=self._th("tyre_cliff_ms", 1500),
                    wear_cliff_pct=self._th("wear_cliff_pct", 70),
                    wear_per_lap=0.0,
                ),
            )
            per_lap = self.fuel_budget.per_lap_kg if self.fuel_budget is not None else 0.0
            burned = (
                max(0.0, self._stint_fuel_ref - state.fuel_in_tank) / per_lap
                if per_lap > 0 and self._stint_fuel_ref > 0
                else 0.0
            )
            predicted = int(
                fit.base_ms
                + fit.deg_ms_per_lap * (state.tyre_age_laps + 1)
                - fit.fuel_ms_per_lap * (burned + 1)
            )
        eb = self.energy_budget
        fb = self.fuel_budget
        state.set_model(
            ModelView(
                deg_fit_source=fit.source if fit is not None else "",
                deg_ms_per_lap=fit.deg_ms_per_lap if fit is not None else 0.0,
                deg_confidence=fit.confidence if fit is not None else 0.0,
                base_pace_ms=fit.base_ms if fit is not None else 0.0,
                laps_of_pace=lop,
                wear_per_lap_pct=self._wear_per_lap,
                pit_loss_s=(
                    self.pit_loss_prior.value / 1000.0 if self.pit_loss_prior is not None else 0.0
                ),
                pit_loss_source=(
                    self.pit_loss_prior.source if self.pit_loss_prior is not None else ""
                ),
                fuel_margin_laps=fb.margin_laps if fb is not None else 0.0,
                fuel_per_lap_kg=fb.per_lap_kg if fb is not None else 0.0,
                fuel_source=fb.source if fb is not None else "",
                energy_per_lap_mj=eb.per_lap_j / 1e6 if eb is not None else 0.0,
                energy_lap_delta_mj=eb.lap_delta_j / 1e6 if eb is not None else 0.0,
                energy_prev_lap_delta_mj=(
                    self.energy_prev_lap.lap_delta_j / 1e6
                    if self.energy_prev_lap is not None
                    else 0.0
                ),
                energy_prev_lap_mode=(
                    self.energy_prev_lap.mode if self.energy_prev_lap is not None else ""
                ),
                energy_laps_to_floor=eb.laps_to_floor if eb is not None else math.inf,
                energy_mode=eb.mode if eb is not None else "",
                predicted_lap_ms=predicted,
            )
        )

    def _plan(self, snap: Snapshot) -> Snapshot:
        """Run the pit-window optimiser on the fresh snapshot and fold the
        plan into both the snapshot and the next ModelView."""
        fit = self.deg_fit
        if not snap.race_phase or self._fresh_prior is None:
            return snap
        if fit is None:
            return self._plans(snap, self._fresh_prior, NO_PLAN)
        fit = planning_fit(fit, self._fresh_prior, self._th("deg_rmse_bad_ms", 800))
        settings = self.store.current()
        ahead = (
            RivalView(
                snap.rival_ahead_idx,
                snap.rival_ahead_name,
                snap.rival_ahead_pace_ms,
                snap.rival_ahead_pitted,
            )
            if snap.rival_ahead_idx >= 0
            else None
        )
        plan = optimise(
            lap_num=snap.lap_num,
            laps_remaining=snap.laps_remaining,
            tyre_age=snap.tyre_age_laps,
            wear_mean=snap.wear_mean_pct,
            fit=fit,
            fresh=self._fresh_prior,
            laps_of_pace=snap.laps_of_pace,
            pit_loss_s=snap.pit_loss_s,
            pit_loss_source=snap.pit_loss_source,
            green_pit_loss_s=self._green_pit_loss_s,
            sc_status=snap.safety_car_status,
            rival_ahead=ahead,
            gap_ahead_s=snap.gap_ahead_s,
            gap_behind_s=snap.gap_behind_s,
            pit_exit_clean=snap.pit_exit_clean,
            restricted=snap.rival_data_restricted,
            mode=self.mode(),
            th=settings.thresholds,
        )
        self.pit_plan = plan
        self.state.set_model(
            dataclasses.replace(
                self.state.model,
                pit_plan=plan.plan,
                pit_plan_lap=plan.lap,
                pit_plan_gain_s=plan.gain_s,
                pit_plan_confidence=plan.confidence,
                pit_plan_risk=plan.risk,
                pit_plan_rival_idx=plan.rival_idx,
                pit_plan_rival_name=plan.rival_name,
                pit_plan_reason=plan.reason,
                pit_window_start=plan.window[0],
                pit_window_end=plan.window[1],
                undercut_s=plan.undercut_s,
                overcut_s=plan.overcut_s,
            )
        )
        snap = dataclasses.replace(
            snap,
            pit_plan=plan.plan,
            pit_plan_lap=plan.lap,
            pit_plan_gain_s=plan.gain_s,
            pit_plan_confidence=plan.confidence,
            pit_plan_risk=plan.risk,
            pit_plan_rival_idx=plan.rival_idx,
            pit_plan_rival_name=plan.rival_name,
            pit_plan_reason=plan.reason,
            pit_window_start=plan.window[0],
            pit_window_end=plan.window[1],
            undercut_s=plan.undercut_s,
            overcut_s=plan.overcut_s,
        )
        return self._plans(snap, fit, plan)

    def _compound_models(self, snap: Snapshot, cur: DegFit) -> tuple[CompoundModel, ...]:
        """Fresh-set pace per dry compound: learned/overlay deg for the set's
        actual compound when known, else the current slope scaled by the
        per-compound factor; sets left from the Tyre Sets packet."""
        settings = self.store.current()
        current = snap.tyre_visual
        names = {16: "soft", 17: "medium", 18: "hard"}
        out: list[CompoundModel] = []
        for v in DRY:
            actual = next((s.actual for s in snap.tyre_sets if s.visual == v and s.actual), 0)
            deg = cur.deg_ms_per_lap * (
                self._th(f"plan_deg_factor_{names[v]}", 1.0)
                / self._th(f"plan_deg_factor_{names.get(current, 'medium')}", 1.0)
            )
            if actual:
                prior = self._compound_priors.get(actual)
                if prior is None:
                    prior = self._deg_prior(snap.track_id, actual, settings)
                    self._compound_priors[actual] = prior
                if prior.source in ("learned", "overlay"):
                    deg = prior.deg_ms_per_lap
            off = self._th(f"plan_pace_{names[v]}_ms", 0.0)
            off_cur = self._th(f"plan_pace_{names[current]}_ms", 0.0) if current in names else 0.0
            sets = (
                sum(1 for s in snap.tyre_sets if s.visual == v and s.available and not s.fitted)
                if snap.tyre_sets
                else -1
            )
            out.append(CompoundModel(v, cur.base_ms + off - off_cur, deg, sets))
        return tuple(out)

    def _plans(self, snap: Snapshot, cur: DegFit, pit: PitPlan) -> Snapshot:
        """Named strategy plans: recompute on each lap / stop / SC change,
        persist set/switch/off events, fold scalars into snapshot + model."""
        phase = snap.race_phase
        if phase == "out_lap" and self._prev_race_phase != "out_lap":
            self._stops_done += 1
        self._prev_race_phase = phase
        tracker = self.plan_tracker
        if phase in ("racing", "sc", "vsc") and snap.tyre_visual and snap.laps_remaining > 0:
            self._used_compounds.add(snap.tyre_visual)
            neutral = phase in ("sc", "vsc")
            cheap = pit.plan == "cheap_stop"
            key = (snap.lap_num, self._stops_done, neutral, cheap, snap.tyre_visual, snap.is_sprint)
            if key != self._plan_key:
                if self._plan_key is not None and self._plan_key[-1] != snap.is_sprint:
                    tracker.reset()  # the weekend format arrived: a fresh plan, not a switch
                self._plan_key = key
                lop = snap.laps_of_pace
                cliff = self._th("tyre_cliff_ms", 1500)
                cliff_age = (
                    snap.tyre_age_laps + lop
                    if math.isfinite(lop)
                    else (cliff / cur.deg_ms_per_lap if cur.deg_ms_per_lap > 0 else math.inf)
                )
                tracker.update(
                    PlanInputs(
                        lap_num=snap.lap_num,
                        laps_remaining=snap.laps_remaining,
                        tyre_age=snap.tyre_age_laps,
                        current=snap.tyre_visual,
                        cur=cur,
                        cur_cliff_age=cliff_age,
                        compounds=self._compound_models(snap, cur),
                        used=frozenset(self._used_compounds),
                        green_loss_s=self._green_pit_loss_s or snap.pit_loss_s,
                        sc_loss_s=self._sc_pit_loss_s or snap.pit_loss_s,
                    ),
                    self._plan_thresholds(snap),
                    stops_done=self._stops_done,
                    neutralised=neutral,
                    cheap_stop=cheap,
                )
                for ev in tracker.drain():
                    self._persist_plan_event(snap, ev, tracker.state)
        st: PlanState = tracker.state
        if not st.active:
            return snap
        fields = view_fields(st, snap.lap_num)
        self.state.set_model(_with_plan(self.state.model, fields))
        return _with_plan(snap, fields)

    def _persist_plan_event(self, snap: Snapshot, ev: PlanEvent, st: PlanState) -> None:
        plans = [
            {"id": p.id, "sequence": p.sequence, "window": list(p.window), "delta_s": p.delta_s}
            for p in st.plans
        ]
        record = {
            "t": snap.now,
            "session_time": snap.session_time,
            "lap": ev.lap,
            "rule_id": None,
            "outcome": "plan",
            "kind": ev.kind,
            "from_plan": ev.from_plan,
            "to_plan": ev.to_plan,
            "reason": ev.reason,
            "delta_s": ev.delta_s,
            "sequence": ev.sequence,
            "plans": plans,
        }
        self.dispatcher.log.write(record)
        if self.db is not None and self.state.session_uid is not None:
            self.db.insert_plan_event(self.state.session_uid, record)

    def battle_rates(self, track_id: int) -> BattleRates:
        """Per-track pass / hold rates from model_params, shrunk to the priors."""
        if self._battle_rates is not None and self._battle_rates[0] == track_id:
            return self._battle_rates[1]
        w = self._th("battle_prior_weight", 4.0)
        priors = {
            PASS_OT: self._th("battle_pass_overtake_prior", 0.6),
            PASS_NO_OT: self._th("battle_pass_no_overtake_prior", 0.6),
            HOLD: self._th("battle_hold_prior", 0.7),
        }
        vals: dict[str, float] = {}
        for name, prior in priors.items():
            p = self.db.get_param(track_id, PASS_COMPOUND, name) if self.db else None
            vals[name] = shrink(p.value if p else None, p.weight if p else 0.0, prior, w)
        rates = BattleRates(vals[PASS_OT], vals[PASS_NO_OT], vals[HOLD])
        self._battle_rates = (track_id, rates)
        return rates

    def _battle(self, snap: Snapshot) -> Snapshot:
        """Battle mode + attack/defend episodes; episode outcomes fold into
        the track's pass model and are written to the decision log."""
        if snap.session_kind != "race" or snap.race_phase != "racing":
            return snap
        attack = self.mode().get("attack_window_s", 1.0)
        own = snap.predicted_lap_ms or int(snap.base_pace_ms)
        inp = BattleInputs(
            now=snap.now,
            lap_num=snap.lap_num,
            position=snap.position,
            laps_remaining=snap.laps_remaining,
            ahead_idx=snap.rival_ahead_idx,
            behind_idx=snap.rival_behind_idx,
            gap_ahead_s=snap.gap_ahead_s,
            gap_behind_s=snap.gap_behind_s,
            trend_ahead_s=snap.gap_trend_ahead_s,
            trend_behind_s=snap.gap_trend_behind_s,
            own_pace_ms=own,
            ahead_pace_ms=snap.rival_ahead_pace_ms,
            behind_pace_ms=snap.rival_behind_pace_ms,
            own_age=snap.tyre_age_laps,
            ahead_age=snap.rival_ahead_age,
            behind_age=snap.rival_behind_age,
            overtake_active=bool(snap.overtake_active)
            if snap.regulations_2026
            else snap.drs_available,
            attack_gap_s=float(attack) if isinstance(attack, int | float) else 1.0,
            positions=tuple(c.car_position for c in snap.cars),
            pitting=frozenset(i for i, c in enumerate(snap.cars) if c.pit_status != 0),
            regulations_2026=snap.regulations_2026,
        )
        tracker = self.battle_tracker
        b: Battle = tracker.update(
            inp, self.store.current().thresholds, self.battle_rates(snap.track_id)
        )
        for ep in tracker.drain():
            self._persist_episode(snap, ep)
        name = ""
        if 0 <= b.result_rival_idx < len(snap.participants):
            name = snap.participants[b.result_rival_idx].name
        return dataclasses.replace(
            snap,
            battle_mode=b.mode,
            battle_mode_laps=b.mode_laps,
            battle_catch_laps=b.catch_laps,
            battle_threat_laps=b.threat_laps,
            battle_closing_ahead_s=b.closing_ahead_s,
            battle_closing_behind_s=b.closing_behind_s,
            battle_tyre_offset_ahead=b.tyre_offset_ahead,
            battle_tyre_offset_behind=b.tyre_offset_behind,
            battle_pass_prob=b.pass_prob,
            battle_hold_prob=b.hold_prob,
            battle_result=b.result,
            battle_result_recent=b.result_recent,
            battle_result_name=name,
            battle_pace_ahead=_rival_pace(snap, snap.rival_ahead_idx, -b.closing_ahead_s),
            battle_pace_behind=_rival_pace(snap, snap.rival_behind_idx, b.closing_behind_s),
        )

    def _persist_episode(self, snap: Snapshot, ep: Episode) -> None:
        self.dispatcher.log.write(
            {
                "t": snap.now,
                "session_time": snap.session_time,
                "lap": ep.end_lap,
                "rule_id": None,
                "outcome": "battle",
                "kind": ep.kind,
                "rival_idx": ep.rival_idx,
                "start_lap": ep.start_lap,
                "overtake": ep.overtake,
                "result": ep.result,
            }
        )
        if self.db is None or snap.track_id < 0 or self._synthetic_session():
            return
        name = HOLD if ep.kind == "defend" else PASS_OT if ep.overtake else PASS_NO_OT
        cap = self._th("param_weight_cap", 50.0)
        self.db.fold_param(
            snap.track_id,
            PASS_COMPOUND,
            name,
            1.0 if ep.success else 0.0,
            1.0,
            param_weight_cap=cap,
        )
        self._battle_rates = None

    def _end_session(self, uid: int, now: float) -> None:
        assert self.db is not None
        self.fold_open_stint()
        self._store_debrief_setup(uid)
        self._session_ended_written = True
        self.db.end_session(uid, now)
        grade_and_store(self.db, uid, self.store.current().thresholds)
        self.db.mark_graded(uid)

    def tick(self, now: float) -> list[Call]:
        self.store.poll(now)
        self.setup_advisor.refresh_rules(self.store.current().setup_rules)
        if self.state.track_id != self._track_loaded:
            self._track_loaded = self.state.track_id
            self.store.set_track(self.state.track_id if self.state.track_id >= 0 else None)
        if (
            self.db is not None
            and self.state.session_uid is not None
            and self.state.track_id >= 0
            and self.state.session_type > 0
            and self._session_upserted != self.state.session_uid
        ):
            self._upsert_session(self.state.session_uid)
        self._write_laps()
        self._update_model()
        snapshot = self._battle(self._plan(self.state.snapshot(now)))
        self._update_setup_stop_wing()
        snapshot = dataclasses.replace(snapshot, **self.setup_advisor.snapshot_fields())
        uid = self.state.session_uid
        if (
            snapshot.session_ended
            and not self._session_ended_written
            and self.db is not None
            and uid is not None
        ):
            self._end_session(uid, now)
            if self.learning_pack_dir is not None:
                from pitwall.learnpack import write_pack

                try:
                    write_pack(
                        self.db,
                        self.learning_pack_dir,
                        keep_days=self.learning_pack_keep_days,
                        refresh_quality=[uid],
                    )
                except (OSError, sqlite3.Error, ValueError) as e:
                    log.warning("learning pack skipped at session end: %s", e)
        press = self.detector.tick(now)
        if press is not None:
            self._press_queue.append(press)
        balance = self.driver_balance(snapshot.lap_num)
        if balance != snapshot.driver_balance:
            snapshot = dataclasses.replace(snapshot, driver_balance=balance)
        for p in self._press_queue:
            if self._menu_press(p, snapshot):
                continue
            if p.kind == "mindset":
                self.cycle_mindset(p.t)
            elif p.kind == "page":
                self.cycle_page(p.t)
            else:
                self.dispatcher.on_press(p, snapshot)
        self._press_queue.clear()
        if self.menu.expired(self.store.current().menu, now):
            self._menu_close(now, snapshot, "timeout")
        self._auto_page(snapshot, now)
        if self.state.last_recv_wall is not None:
            self.metrics.note_packet_to_snapshot(self.state.last_recv_wall, now)
        if snapshot.paused != self._paused:
            self.dispatcher.log.write(
                {"t": now, "outcome": "paused" if snapshot.paused else "resumed"}
            )
            if snapshot.paused:
                self.dispatcher.purge(reason="paused", now=now)
            self._paused = snapshot.paused
        if snapshot.paused:
            return []
        self._heartbeat(now)
        if self.recovering:
            if self.rule_engine is not None:
                # Arm/disarm edges on the rebuilt state so conditions already
                # true before the crash don't all fire on the first live tick.
                self.rule_engine.evaluate(snapshot)
            return []
        if self.rule_engine is not None:
            result = self.rule_engine.evaluate(snapshot)
            self.dispatcher.submit(result.candidates, snapshot)
        return self.dispatcher.drain(now)

    def _heartbeat(self, now: float) -> None:
        period = self.store.current().engine.heartbeat_s
        if self.db is None or self.state.session_uid is None or period <= 0:
            return
        if self._last_heartbeat is not None and now - self._last_heartbeat < period:
            return
        self._last_heartbeat = now
        path = self.recording_path_source() if self.recording_path_source is not None else None
        self.db.write_heartbeat(
            self.state.session_uid,
            float(self.state.last_packet_t or 0.0),
            time.time(),
            str(path or ""),
            self.state.lap_num,
        )

    def recover(self, wall_now: float | None = None) -> str | None:
        """Mid-session restart: if the SQLite heartbeat is fresh and names a
        recording, replay that recording's tail through ingest (not the
        recorder, rules muted) so EMAs, laps and phase are rebuilt; the model
        reads its lap history from the committed SQLite rows. Returns a short
        description, or None when there is nothing to recover."""
        if self.db is None:
            return None
        hb = self.db.read_heartbeat()
        eng = self.store.current().engine
        wall_now = time.time() if wall_now is None else wall_now
        if hb is None or not hb.recording_path or wall_now - hb.wall_t > eng.recovery_max_age_s:
            return None
        path = Path(hb.recording_path)
        if not path.is_file():
            return None
        tail: collections.deque[tuple[int, bytes]] = collections.deque()
        horizon_us = int(eng.recovery_tail_s * 1e6)
        try:
            with RecordingReader(path) as reader:
                for offset_us, payload in reader:
                    tail.append((offset_us, payload))
                    while tail and offset_us - tail[0][0] > horizon_us:
                        tail.popleft()
        except ValueError:
            pass  # a crash leaves a truncated final record; keep what parsed
        if not tail:
            return None
        recorder, self.ingest.recorder = self.ingest.recorder, None
        self.recovering = True
        period = self.tick_period
        try:
            next_tick = tail[0][0] / 1e6
            for offset_us, payload in tail:
                t = offset_us / 1e6
                self.ingest.on_datagram(payload, t)
                while t >= next_tick:
                    self.tick(next_tick)
                    next_tick += period
        finally:
            self.recovering = False
            self.ingest.recorder = recorder
            self.dispatcher.purge(reason="recovery", now=self.clock.now())
        if self.state.session_uid is not None and self.state.session_uid != hb.session_uid:
            return None
        self._session_upserted = self.state.session_uid
        self.dispatcher.log.write(
            {
                "t": self.clock.now(),
                "outcome": "recovered",
                "rule_id": None,
                "text": f"{path.name} tail {len(tail)} datagrams, lap {self.state.lap_num}",
            }
        )
        return f"recovered lap {self.state.lap_num} from {path.name} ({len(tail)} datagrams)"

    def rejoin_text(self) -> str:
        """Spoken after a successful recover()."""
        st = self.state
        parts = ["Back with you."]
        if st.lap_num > 0:
            parts.append(f"Lap {st.lap_num}" + (f", P{st.position}." if st.position else "."))
        if st.total_laps > 0 and st.lap_num > 0:
            parts.append(f"{max(0, st.total_laps - st.lap_num + 1)} to go.")
        return " ".join(parts)

    async def run_live(self) -> None:
        """Tick at tick_hz forever; dispatch drains on its own loop. The
        watchdog keeps the loop alive through a failing tick (logged)."""
        period = self.tick_period
        while True:
            if self.alive_path is not None and time.monotonic() - self._alive_t >= 1.0:
                self._alive_t = time.monotonic()
                with contextlib.suppress(OSError):
                    self.alive_path.touch()
            try:
                self.tick(self.clock.now())
            except Exception:  # noqa: BLE001 - watchdog: one bad tick must not end a race
                self.tick_errors += 1
                self.dispatcher.log.write(
                    {
                        "t": self.clock.now(),
                        "outcome": "tick_error",
                        "rule_id": None,
                        "text": traceback.format_exc(limit=3),
                    }
                )
            await self.clock.sleep(period)


class _ReplaySink:
    """Wraps ingest; ticks the engine when the record clock crosses a tick
    boundary. recv_time is the record's own timestamp in seconds."""

    def __init__(self, ingest: Ingest, engine: Engine) -> None:
        self.ingest = ingest
        self.engine = engine
        self.next_tick: float | None = None
        self.calls: list[Call] = []

    def on_datagram(self, payload: bytes, recv_time: float) -> None:
        self.ingest.on_datagram(payload, recv_time)
        period = self.engine.tick_period
        if self.next_tick is None:
            self.next_tick = recv_time
        while recv_time >= self.next_tick:
            self.calls.extend(self.engine.tick(self.next_tick))
            self.next_tick += period

    def finish(self) -> None:
        self.calls.extend(self.engine.dispatcher.drain(self.engine.clock.now()))


async def run_replay(
    path: Path,
    engine: Engine,
    speed: float | None = None,
    *,
    from_us: int | None = None,
    to_us: int | None = None,
) -> tuple[int, list[Call]]:
    """Stream a recording through ingest + engine. Returns (datagrams, calls)."""
    from pitwall.net.replay import replay

    sink = _ReplaySink(engine.ingest, engine)
    delivered = await replay(path, sink, engine.clock, speed, from_us=from_us, to_us=to_us)
    sink.finish()
    return delivered, sink.calls


def build_engine(
    *,
    clock: Clock | None = None,
    overrides: dict[str, Any] | None = None,
    rules_dir: Path | None = None,
    isolated: bool = False,
    synthetic: bool = False,
    decision_log_path: Path | None = None,
    decision_log_fp: Any = None,
    sinks: list[CallSink] | None = None,
    record_to: Path | None = None,
    db: Any = None,
    session_started_at: float | None = None,
    learning_pack_dir: Path | None = None,
    learning_pack_keep_days: int = 30,
) -> Engine:
    """Assemble a full engine from the layered config. db=None disables
    SQLite mirroring (replays opt in via the CLI)."""
    store = ConfigStore(overrides=overrides, rules_dir=rules_dir, isolated=isolated)
    settings = store.current()
    clock = clock or WallClock()
    recorder = None
    if record_to is not None:
        from pitwall.net.recording import RecordingRotator

        recorder = RecordingRotator(record_to, config_hash=int(store.hash, 16) % (2**32))
    ingest = Ingest(recorder=recorder)
    state = SessionState(
        ema_fast_s=settings.engine.ema_fast_s,
        ema_slow_s=settings.engine.ema_slow_s,
        straight_hold_s=settings.engine.straight_hold_s,
        press_bit=settings.input.udp_action_bit,
        toggle_bit=settings.input.silent_toggle_bit,
        action_bits=action_bits(settings.input),
        thresholds=settings.thresholds,
    )
    state.register(ingest)
    mode = store.current().resolved_mindset()
    rule_engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=mode,
        staleness_s=settings.engine.staleness_s,
    )
    log_path = decision_log_path
    if log_path is None and decision_log_fp is None:
        log_path = Path(settings.recording.directory) / "decisions.jsonl"
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    dlog = DecisionLog(
        log_path,
        fp=decision_log_fp,
        config_hash=store.hash,
        mindset=settings.mindset.active,
        db=db,
        session_uid_source=lambda: state.session_uid,
    )
    dispatcher = Dispatcher(
        settings.policy,
        clock,
        decision_log=dlog,
        sinks=sinks if sinks is not None else [LogSink()],
        metrics=Metrics(),
        budget_override=mode.get("call_budget_per_lap"),
        input=settings.input,
    )
    engine = Engine(
        store,
        clock,
        ingest,
        state,
        rule_engine,
        dispatcher,
        learning_pack_dir=learning_pack_dir,
        learning_pack_keep_days=learning_pack_keep_days,
        synthetic_source=synthetic,
    )
    engine.session_origin_started_at = session_started_at
    return engine


def build_census_engine(clock: Clock | None = None) -> Engine:
    """Ingest-only engine for --no-rules census replays."""
    store = ConfigStore()
    clock = clock or VirtualClock()
    ingest = Ingest()
    state = SessionState()
    dlog = DecisionLog()
    dispatcher = Dispatcher(store.current().policy, clock, decision_log=dlog, sinks=[])
    return Engine(store, clock, ingest, state, None, dispatcher)
