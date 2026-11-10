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
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, CallSink, Dispatcher, LogSink
from pitwall.clock import Clock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import (
    InputSettings,
    MenuItemModel,
    Settings,
    call_budget,
    resolve_mindset,
)
from pitwall.config.paths import storage_path
from pitwall.config.thresholds import threshold
from pitwall.engine.context import EngineContext
from pitwall.engine.model import SessionModel
from pitwall.ingest import Ingest
from pitwall.input.menu import DriverMenu, ReplyPicker, answer
from pitwall.input.press import Press, PressDetector
from pitwall.learning.hindsight import grade_and_store
from pitwall.learning.tune import load_cooldown_mults
from pitwall.metrics import Metrics
from pitwall.model.budget import EnergyBudget, FuelBudget
from pitwall.model.deg import (
    DegFit,
    Prior,
    cliff_age,
    fit_stint,
    planning_fit,
    usable_laps,
)
from pitwall.model.driver import (
    DriverScale,
    advice_scale,
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
from pitwall.state.session import SessionState, Snapshot
from pitwall.store.db import LapRow, lap_row_from_summary
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
)
from pitwall.strategy.montecarlo import McResult, plan_agreement, scale_plan
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
    compound_stop_owed,
    view_fields,
)
from pitwall.voice.channel import ChannelRecord, VoiceChannel
from pitwall.voice.grammar import VoiceGrammar
from pitwall.voice.worker import VoiceWorker, make_voice_worker

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


def _true_behind_view(snap: Snapshot) -> tuple[RivalView | None, float]:
    idx = snap.true_rival_behind_idx
    if idx < 0:
        return None, math.inf
    pace_ms = snap.rival_behind_pace_ms if idx == snap.rival_behind_idx else 0
    return (
        RivalView(
            idx,
            snap.true_rival_behind_name,
            pace_ms,
            snap.true_rival_behind_pitted,
        ),
        snap.true_rival_behind_gap_s,
    )


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


# Pit plans that end in a stop. "stay" and "no_stop" are plans too, but the
# wing change at the stop only makes sense when a stop is coming.
_STOP_PLANS = frozenset(
    {"box_now", "box_in_n", "cheap_stop", "free_stop", "undercut", "overcut", "cover"}
)


def _hold_cover_plan(
    held: tuple[PitPlan, int] | None,
    plan: PitPlan,
    snap: Snapshot,
    pit_min_laps_left: float,
) -> tuple[PitPlan, tuple[PitPlan, int] | None]:
    if plan.plan in ("cover", "defend"):
        if held is None or held[0].rival_idx != plan.rival_idx:
            held = (plan, snap.lap_num)
        return plan, held
    if held is None:
        return plan, None
    held_plan, start_lap = held
    if (
        snap.phase == "racing"
        and snap.safety_car_status == 0
        and snap.lap_num <= start_lap + 1
        and snap.laps_remaining > pit_min_laps_left
        and plan.plan != "cheap_stop"
    ):
        return held_plan, held
    return plan, None


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
class _PendingPitLoss:
    session_uid: int
    track_id: int
    in_lap: LapRow
    out_lap: LapRow
    ref_pace_ms: int
    lane_ms: int
    neutralised: int


def budget_label(v: float) -> str:
    if v == 0:
        return "silence"
    if math.isinf(v):
        return "no limit"
    return str(int(v))


class Engine:
    def __init__(
        self,
        store: ConfigStore,
        clock: Clock,
        ingest: Ingest,
        state: SessionState,
        rule_engine: RuleEngine | None,
        dispatcher: Dispatcher,
        synthetic_source: bool = False,
    ) -> None:
        self.ctx = EngineContext(store, clock, state, dispatcher)
        self.ctx.mode_source = lambda: self.mode()
        self.ctx.store = store
        settings = store.current()
        self.setup_advisor = SetupAdvisor(state, settings.setup_rules)
        self.ctx.clock = clock
        self.ingest = ingest
        self.ctx.state = state
        self.ctx.synthetic_source = synthetic_source
        self.rule_engine = rule_engine
        self.ctx.dispatcher = dispatcher
        self.ctx.dispatcher.on_response = self._on_dispatcher_response
        self.metrics = dispatcher.metrics
        self.speaker_name = "null"
        self.recording_desc = "off"
        self.ctx.voice = None
        self._voice_worker: VoiceWorker | None = None
        self._voice_closed: list[ChannelRecord] = []
        self.ctx.on_voice_event = None
        self.ctx.on_session_end = None
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
        self.ctx.mindset_override = None
        self.ctx.budget_live = None  # driver-menu calls-per-lap override
        pages = store.current().ui.pages
        self.ctx.page = pages[0] if pages else "race"
        self.ctx.last_snapshot = None  # latest processed tick snapshot
        # Driver -> pit wall menu (docs/12); opinions bias advice for a few laps.
        self.ctx.menu = DriverMenu()
        self._menu_replies = ReplyPicker()
        self._menu_signal_names: dict[str, frozenset[str]] = {}
        self._menu_signal_hash = self.ctx.store.hash
        self.ctx.opinions = {}  # topic -> (item id, lap)
        self.ctx.db = dispatcher.log.db
        self.ctx.driver_db = self.ctx.db
        self.model = SessionModel(self.ctx, self._apply_mode, lambda: self._laps_written)
        self._apply_mode()
        # Crash recovery (docs/18): heartbeat to SQLite; on restart replay the
        # recording tail with the rules muted.
        self.recording_path_source: Any = None  # callable -> Path | None
        self._last_heartbeat: float | None = None
        self.ctx.recovering = False
        self.alive_path: Path | None = None  # touched each live loop for the supervisor
        self._alive_t = -math.inf
        self._rule_errors_logged: set[tuple[str, str]] = set()
        self.ctx.dispatcher.tuned_cooldown = load_cooldown_mults(self.ctx.db)
        self._laps_written = 0
        self._session_upserted: int | None = None
        self._parc_ferme_written: int | None = None
        self._weekend_structure_written: tuple[int, ...] = ()
        self.session_origin_started_at: float | None = None
        self._track_loaded: int | None = None
        self._session_ended_written = False
        # M3 model outputs, exposed on the engine until H2 moves them into
        # the snapshot.
        self.ctx.deg_fit = None
        self.ctx.pit_loss_prior = None
        self.ctx.pit_plan = NO_PLAN
        self._held_plan: tuple[PitPlan, int] | None = None
        self.ctx.fresh_prior = None
        self.ctx.green_pit_loss_s = 0.0
        self.ctx.green_pit_samples_s = []
        self.ctx.sc_pit_loss_s = 0.0
        self._plan_mc_cache: dict[tuple[Any, ...], McResult | None] = {}
        # Named strategy plans (docs/03 "Strategy plans").
        self.plan_tracker = PlanTracker()
        self._plan_key: tuple[Any, ...] | None = None
        self._damage_pace_ms = 0.0
        self._damage_stops = 0
        self._stops_done = 0
        self._red_restart = False
        self._prev_pit_status = 0
        self._stop_expect: tuple[str, str, int] | None = None
        self._stop_check: tuple[str, str, int, float] | None = None
        self._used_compounds: set[int] = set()
        self._prev_race_phase = ""
        # Battle state + learned pass model (docs/18 Battle state).
        self.battle_tracker = BattleTracker()
        self.ctx.fuel_budget = None
        self.ctx.energy_budget = None
        self.ctx.energy_prev_lap = None
        self._pit_in_lap: LapSummary | None = None
        self._pending_pit_loss: _PendingPitLoss | None = None
        self._folded_stints: set[tuple[int, int]] = set()
        self.ctx.stint_fuel_ref = 0.0
        self.ctx.stint_laps = []

        def _on_rewind(t: float) -> None:
            dispatcher.purge(reason="flashback", now=t)
            self.model.rewind(t)
            self._held_plan = None
            self._stop_expect = None
            self._prev_pit_status = -1  # -1: pit state unknown until the next tick
            if self._stop_check is not None and t < self._stop_check[3]:
                self._stop_check = None

        state.rewind_listeners.append(_on_rewind)
        state.session_listeners.append(self._on_new_session)
        state.session_end_listeners.append(self.fold_session_end)

    def battle_rates(self, track_id: int) -> BattleRates:
        return self.model.battle_rates(track_id)

    @property
    def store(self) -> ConfigStore:
        return self.ctx.store

    @store.setter
    def store(self, value: ConfigStore) -> None:
        self.ctx.store = value

    @property
    def clock(self) -> Clock:
        return self.ctx.clock

    @clock.setter
    def clock(self, value: Clock) -> None:
        self.ctx.clock = value

    @property
    def state(self) -> SessionState:
        return self.ctx.state

    @state.setter
    def state(self, value: SessionState) -> None:
        self.ctx.state = value

    @property
    def dispatcher(self) -> Dispatcher:
        return self.ctx.dispatcher

    @dispatcher.setter
    def dispatcher(self, value: Dispatcher) -> None:
        self.ctx.dispatcher = value

    @property
    def db(self) -> Any:
        return self.ctx.db

    @db.setter
    def db(self, value: Any) -> None:
        self.ctx.db = value

    @property
    def driver_db(self) -> Any:
        return self.ctx.driver_db

    @driver_db.setter
    def driver_db(self, value: Any) -> None:
        self.ctx.driver_db = value

    @property
    def synthetic_source(self) -> bool:
        return self.ctx.synthetic_source

    @synthetic_source.setter
    def synthetic_source(self, value: bool) -> None:
        self.ctx.synthetic_source = value

    @property
    def recovering(self) -> bool:
        return self.ctx.recovering

    @recovering.setter
    def recovering(self, value: bool) -> None:
        self.ctx.recovering = value

    @property
    def deg_fit(self) -> DegFit | None:
        return self.ctx.deg_fit

    @deg_fit.setter
    def deg_fit(self, value: DegFit | None) -> None:
        self.ctx.deg_fit = value

    @property
    def pit_loss_prior(self) -> Prior | None:
        return self.ctx.pit_loss_prior

    @pit_loss_prior.setter
    def pit_loss_prior(self, value: Prior | None) -> None:
        self.ctx.pit_loss_prior = value

    @property
    def pit_plan(self) -> PitPlan:
        return self.ctx.pit_plan

    @pit_plan.setter
    def pit_plan(self, value: PitPlan) -> None:
        self.ctx.pit_plan = value

    @property
    def fuel_budget(self) -> FuelBudget | None:
        return self.ctx.fuel_budget

    @fuel_budget.setter
    def fuel_budget(self, value: FuelBudget | None) -> None:
        self.ctx.fuel_budget = value

    @property
    def energy_budget(self) -> EnergyBudget | None:
        return self.ctx.energy_budget

    @energy_budget.setter
    def energy_budget(self, value: EnergyBudget | None) -> None:
        self.ctx.energy_budget = value

    @property
    def energy_prev_lap(self) -> EnergyBudget | None:
        return self.ctx.energy_prev_lap

    @energy_prev_lap.setter
    def energy_prev_lap(self, value: EnergyBudget | None) -> None:
        self.ctx.energy_prev_lap = value

    @property
    def mindset_override(self) -> str | None:
        return self.ctx.mindset_override

    @mindset_override.setter
    def mindset_override(self, value: str | None) -> None:
        self.ctx.mindset_override = value

    @property
    def budget_live(self) -> float | None:
        return self.ctx.budget_live

    @budget_live.setter
    def budget_live(self, value: float | None) -> None:
        self.ctx.budget_live = value

    @property
    def page(self) -> str:
        return self.ctx.page

    @page.setter
    def page(self, value: str) -> None:
        self.ctx.page = value

    @property
    def menu(self) -> DriverMenu:
        return self.ctx.menu

    @menu.setter
    def menu(self, value: DriverMenu) -> None:
        self.ctx.menu = value

    @property
    def opinions(self) -> dict[str, tuple[str, int]]:
        return self.ctx.opinions

    @opinions.setter
    def opinions(self, value: dict[str, tuple[str, int]]) -> None:
        self.ctx.opinions = value

    @property
    def voice(self) -> VoiceChannel | None:
        return self.ctx.voice

    @voice.setter
    def voice(self, value: VoiceChannel | None) -> None:
        self.ctx.voice = value

    @property
    def on_voice_event(self) -> Callable[[dict[str, Any]], None] | None:
        return self.ctx.on_voice_event

    @on_voice_event.setter
    def on_voice_event(self, value: Callable[[dict[str, Any]], None] | None) -> None:
        self.ctx.on_voice_event = value

    @property
    def on_session_end(self) -> Callable[[int], None] | None:
        return self.ctx.on_session_end

    @on_session_end.setter
    def on_session_end(self, value: Callable[[int], None] | None) -> None:
        self.ctx.on_session_end = value

    @property
    def last_snapshot(self) -> Snapshot | None:
        return self.ctx.last_snapshot

    @last_snapshot.setter
    def last_snapshot(self, value: Snapshot | None) -> None:
        self.ctx.last_snapshot = value

    def _on_dispatcher_response(self, rule_id: str, answer: str, _snapshot: Snapshot) -> None:
        if rule_id == "cool_push_check":
            self.ctx.state.answer_push_check(answer == "ack")

    @property
    def tick_period(self) -> float:
        return 1.0 / self.ctx.store.current().engine.tick_hz

    def _on_press_edge(self, t: float, down: bool) -> None:
        press = self.detector.edge(t, down)
        if press is not None:
            self._press_queue.append(press)

    def setup_voice(self) -> str | None:
        voice = self.ctx.store.current().voice
        if not voice.enabled:
            return None
        if sys.platform != "win32":
            log.warning("voice needs Windows SAPI; disabled")
            print("voice: needs Windows SAPI; disabled", file=sys.stderr)
            return "off (needs Windows)"
        worker: VoiceWorker | None = None
        try:
            worker = make_voice_worker(voice)
            if not worker.wait_ready(10.0):
                raise worker.error or RuntimeError("recogniser did not become ready")
            self.enable_voice(worker)
        except Exception as exc:  # noqa: BLE001 - log voice startup failure and disable voice
            if worker is not None:
                worker.close()
            log.warning("voice unavailable (%s); disabled", exc)
            return f"off ({exc})"
        rec: Any = worker.recognizer
        return f"{rec.recognizer_desc}, mic {rec.device_desc}"

    def enable_voice(self, worker: VoiceWorker) -> None:
        settings = self.ctx.store.current().voice
        grammar = VoiceGrammar.from_mapping(settings.intents)
        self._voice_worker = worker
        self.ctx.voice = VoiceChannel(
            worker,
            grammar.intent_for,
            max_open_s=settings.max_open_s,
            confidence_min=settings.confidence_min,
        )
        self.ctx.voice.on_close = self._voice_closed.append

    def close_voice(self) -> None:
        if self._voice_worker is not None:
            self._voice_worker.close()
            self._voice_worker = None

    def _voice_open(self, t: float, via: str, max_open_s: float | None = None) -> None:
        channel = self.ctx.voice
        if channel is None:
            return
        cap_s = max_open_s or channel.max_open_s
        if channel.open(t, via=via, max_open_s=cap_s) and self.ctx.on_voice_event is not None:
            self.ctx.on_voice_event({"state": "listening", "via": via, "cap_s": cap_s})

    def _voice_close(self, t: float, reason: str) -> None:
        if self.ctx.voice is not None:
            self.ctx.voice.close(t, reason)

    def _voice_tick(self, now: float, snapshot: Snapshot) -> None:
        channel = self.ctx.voice
        worker = self._voice_worker
        if channel is not None and worker is not None:
            for name, text, confidence, _ in worker.drain():
                if name == "recognised":
                    channel.recognised(text, confidence, now)
                elif name == "false":
                    channel.false_recognition(text, confidence, now)
                else:
                    channel.mark(name, now)
            channel.tick(now)
        closed = list(self._voice_closed)
        self._voice_closed.clear()
        for record in closed:
            self._voice_done(record, snapshot)

    def _voice_done(self, record: ChannelRecord, snapshot: Snapshot) -> None:
        items = self.ctx.store.current().menu.items
        item = next((entry for entry in items if entry.id == record.intent), None)
        if self.ctx.on_voice_event is not None:
            self.ctx.on_voice_event(
                {
                    "state": "closed",
                    "reason": record.reason,
                    "text": record.text,
                    "intent": record.intent,
                    "via": record.via,
                    "item": item.label if item is not None else None,
                }
            )
        if record.reason != "recognised" or record.intent is None:
            return
        if item is not None:
            if self.ctx.menu.open:
                self.ctx.menu.close()
                self.ctx.dispatcher.cancel_menu_prompt()
                self._menu_log(record.closed_t, snapshot, "menu_close", None, "voice")
            self._menu_answer(item, record.closed_t, snapshot, via="voice")
        elif record.intent in ("ack", "neg"):
            self.ctx.dispatcher.on_press(Press(record.intent, record.closed_t), snapshot)
        elif record.intent in ("mindset_aggressive", "mindset_balanced"):
            self.set_mindset(record.intent.removeprefix("mindset_"), record.closed_t)
        elif record.intent == "page":
            self.reset_page(record.closed_t)

    def voice_payload(self, now: float) -> dict[str, Any]:
        channel = self.ctx.voice
        if channel is None or not channel.is_open or channel.current is None:
            return {
                "available": channel is not None,
                "listening": False,
                "via": "",
                "left_s": None,
                "warn": False,
            }
        elapsed = channel.open_for(now)
        return {
            "available": True,
            "listening": True,
            "via": channel.current.via,
            "left_s": max(0.0, channel.current.cap_s - elapsed),
            "warn": elapsed >= self.ctx.store.current().voice.open_warn_s,
        }

    # -- live overrides: mindset (UDP Action 2) and dashboard page (Action 4) --

    @property
    def mindset(self) -> str:
        return self.ctx.mindset_override or self.ctx.store.current().mindset.active

    def _driver_scale(
        self,
        settings: Settings | None = None,
        raw_mode: Mapping[str, Any] | None = None,
    ) -> DriverScale:
        cfg = self.ctx.store.current() if settings is None else settings
        base_mode = resolve_mindset(cfg.mindsets, self.mindset) if raw_mode is None else raw_mode
        rating, conf, source = self.ctx.driver_rating
        return advice_scale(
            rating,
            conf,
            cfg.thresholds,
            base_mode,
            aggressive=self.mindset == "aggressive",
            source=source,
        )

    def mode(self) -> dict[str, Any]:
        settings = self.ctx.store.current()
        resolved = resolve_mindset(settings.mindsets, self.mindset)
        if self.mindset == "aggressive":
            drv = self._driver_scale(settings, resolved)
            resolved = {
                **resolved,
                "pit_confidence_min": drv.pit_confidence_min,
                "position_loss_risk_max": drv.position_loss_risk_max,
            }
        return resolved

    def _apply_mode(self) -> None:
        mode = self.mode()
        self.ctx.state.set_mode_offsets(
            thermal_warn_offset_c=float(mode.get("thermal_warn_offset_c", 0) or 0),
            sector_loss_call_ms=float(mode.get("sector_loss_call_ms", 100) or 100),
        )
        if self.rule_engine is not None:
            self.rule_engine.mode = mode
        budget = (
            self.ctx.budget_live
            if self.ctx.budget_live is not None
            else mode.get("call_budget_per_lap")
        )
        self.ctx.dispatcher.budget_override = call_budget(budget)
        self.ctx.dispatcher.log.mindset = self.mindset

    def set_mindset(self, name: str, now: float) -> bool:
        if name not in self.ctx.store.current().mindsets or name == self.mindset:
            return False
        self.ctx.mindset_override = name
        self._apply_mode()
        snap = self.ctx.dispatcher.latest_snapshot or self.ctx.state.snapshot(now)
        self.ctx.dispatcher.announce_mindset(name, dataclasses.replace(snap, now=now))
        return True

    def cycle_budget(self) -> float:
        """Next driver-menu calls-per-lap step; overrides the mindset's budget."""
        raw_steps = self.ctx.store.current().menu.budget_steps or [0, 15, "unlimited"]
        steps = [budget for step in raw_steps if (budget := call_budget(step)) is not None]
        cur = self.ctx.dispatcher.budget_override
        nxt = next((s for s in steps if cur is None or s > cur), steps[0])
        self.ctx.budget_live = nxt
        self._apply_mode()
        if nxt == 0:
            self.ctx.dispatcher.drop_budgeted()
        return nxt

    def cycle_mindset(self, now: float) -> None:
        cycle = [m for m in self.ctx.store.current().input.mindset_cycle if m]
        if not cycle:
            return
        cur = self.mindset
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
        self.set_mindset(nxt, now)

    def set_page(self, name: str, now: float, *, manual: bool = True) -> bool:
        if name not in self.ctx.store.current().ui.pages:
            return False
        if name == self.ctx.page:
            return False
        self.ctx.page = name
        self.ctx.dispatcher.log.write(
            {"t": now, "outcome": "page", "rule_id": None, "text": name, "manual": manual}
        )
        return True

    def reset_page(self, now: float) -> None:
        pages = self.ctx.store.current().ui.pages
        self.set_page(pages[0] if pages else "race", now)

    def _auto_page(self, snap: Snapshot, now: float) -> None:
        """Contextual page (off by default). A page never changes under a
        fresh call banner."""
        ui = self.ctx.store.current().ui
        if not ui.auto_page:
            return
        last = self.ctx.dispatcher.last_call_t
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
        {"type":"page","name"} (no name resets to the first page) and
        {"type":"menu","op":"up"|"down"|"confirm"|"close"} or {"type":"voice"}."""
        now = self.ctx.clock.now()
        if msg.get("type") == "voice":
            if self.ctx.voice is None:
                return
            if self.ctx.voice.is_open:
                self._voice_close(now, "tap")
            else:
                self._voice_open(now, "key", self.ctx.store.current().voice.key_max_open_s)
        elif msg.get("type") == "mindset":
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
                self.reset_page(now)
        elif msg.get("type") == "menu":
            op = msg.get("op")
            kind = _MENU_CLIENT_OPS.get(op) if isinstance(op, str) else None
            if kind is not None:
                self._press_queue.append(Press(kind, now))

    def client_press(self, down: bool) -> None:
        """WebSocket/spacebar press path (docs/12): feed the same detector."""
        self._on_press_edge(self.ctx.clock.now(), down)

    # -- driver menu (docs/12) -------------------------------------------------

    def menu_payload(self, now: float) -> dict[str, object]:
        return self.ctx.menu.payload(self.ctx.store.current().menu, now)

    def _menu_press(self, p: Press, snapshot: Snapshot) -> bool:
        """Route a press through the menu. False: not a menu press; the
        caller handles it as usual."""
        settings = self.ctx.store.current().menu
        kind = p.kind
        if kind.startswith("item:"):
            item = next((i for i in settings.items if i.id == kind[5:]), None)
            if item is not None:
                self._menu_close(p.t, snapshot, "shortcut")
                self._menu_answer(item, p.t, snapshot, "shortcut")
            return True
        if self.ctx.menu.open and kind in ("page", "mindset"):
            remap = self.ctx.store.current().input.menu_open_actions
            op = remap.page if kind == "page" else remap.mindset
            if op:
                kind = f"menu_{op}"
        if kind in ("menu_up", "menu_down"):
            was_open = self.ctx.menu.open
            item = self.ctx.menu.step(
                settings, -1 if kind == "menu_up" else 1, p.t, self._menu_ns(snapshot)
            )
            if item is None:
                return True
            if not was_open:
                self._menu_log(p.t, snapshot, "menu_open", None, "")
                self._voice_open(p.t, "menu")
            if settings.speak_on_scroll:
                self.ctx.dispatcher.menu_prompt(item.label, snapshot)
            return True
        if kind == "menu_close":
            self._menu_close(p.t, snapshot, "close")
            return True
        if not self.ctx.menu.open:
            return kind == "menu_confirm"
        if kind in ("ack", "menu_confirm"):
            self._menu_confirm(p.t, snapshot)
            return True
        if kind in ("neg", "bookmark"):
            self._menu_close(p.t, snapshot, "cancel")
            return True
        return False

    def _plan_thresholds(self, snap: Snapshot) -> Mapping[str, object]:
        th: dict[str, object] = {**self.ctx.store.current().thresholds, **self.mode()}
        if not snap.is_sprint:
            return th
        return {**th, "plan_two_compound_rule": 0}  # no mandatory stop in a sprint

    def _menu_ns(self, snapshot: Snapshot) -> dict[str, Any]:
        cfg = self.ctx.store.current()
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
        if not self.ctx.menu.open:
            return
        self._voice_close(t, "menu")
        self.ctx.menu.close()
        self.ctx.dispatcher.cancel_menu_prompt()
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
        self.ctx.dispatcher.log.write(
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
        self._voice_close(t, "menu")
        item = self.ctx.menu.selected(self.ctx.store.current().menu)
        self.ctx.menu.close()
        self.ctx.dispatcher.cancel_menu_prompt()
        if item is not None:
            self._menu_answer(item, t, snapshot, "menu")

    def _menu_signal_values(
        self, item: MenuItemModel, snapshot: Snapshot
    ) -> dict[str, int | float]:
        current_hash = self.ctx.store.hash
        if self._menu_signal_hash != current_hash:
            self._menu_signal_names.clear()
            self._menu_signal_hash = current_hash
        names = self._menu_signal_names.get(item.id)
        if names is None:
            rules = {rule.id: rule for rule in self.ctx.store.current().rules}
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
            budget = self.cycle_budget()
            values["budget"] = budget_label(budget)
            if budget == 0:
                case = "silence"
            elif math.isinf(budget):
                case = "unlimited"
        text = self._menu_replies.pick(item, case, values)
        inputs: dict[str, Any] = {"case": case, "via": via, **values}
        if item.kind == "question" and item.related_rules:
            inputs["signals"] = self._menu_signal_values(item, snap)
        self._menu_log(t, snap, "driver_input", item, text, inputs)
        if item.kind == "opinion" and item.topic:
            self.ctx.opinions[item.topic] = (item.id, snapshot.lap_num)
        if item.action == "mindset":
            self.cycle_mindset(t)
        elif item.action == "silent":
            self.ctx.dispatcher.toggle_silent(snap)
        elif item.action == "page":
            self.reset_page(t)
        if text:
            self.ctx.dispatcher.menu_reply(text, f"menu:{item.id}", snap, item.related_rules)

    def driver_balance(self, lap: int) -> str:
        """Latest balance opinion while it still holds (docs/12 advice bias)."""
        held = self.ctx.opinions.get("balance")
        hold = self.ctx.store.current().menu.opinion_hold_laps
        if held is None or lap - held[1] > hold:
            return ""
        return held[0]

    def _on_new_session(self, uid: int) -> None:
        pending = self._pending_pit_loss
        if pending is not None:
            if self.ctx.db is None:
                self._pending_pit_loss = None
            else:
                try:
                    laps = self.ctx.db.laps_for(pending.session_uid, 0)
                    self._flush_pending_pit_loss(pending.session_uid, laps, force=True)
                except sqlite3.Error:
                    self._pending_pit_loss = None
        self.ctx.dispatcher.reset_session(uid)
        self._held_plan = None
        self._rule_errors_logged.clear()
        self.model._reset_energy_lap_tracking()
        self.ctx.menu.close()
        self._voice_close(self.ctx.clock.now(), "session")
        self._menu_replies.reset()
        self.ctx.opinions.clear()
        self._laps_written = len(self.ctx.state.laps)
        self._session_ended_written = False
        self._pit_in_lap = None
        self._folded_stints.clear()
        self.model.reset()
        self.ctx.deg_fit = None
        self.plan_tracker.reset()
        self._plan_key = None
        self._damage_pace_ms = 0.0
        self._damage_stops = 0
        self._plan_mc_cache.clear()
        self.battle_tracker.reset()
        self._stops_done = 0
        self._red_restart = False
        self._prev_pit_status = 0
        self._stop_expect = None
        self._stop_check = None
        self.ctx.stint_fuel_ref = 0.0
        self._used_compounds.clear()
        self._prev_race_phase = ""
        self.session_origin_started_at = None
        self._parc_ferme_written = None
        self._weekend_structure_written = ()
        self.setup_advisor.reset(uid)

    def _evaluate_live_setup(self, uid: int) -> None:
        self.setup_advisor.evaluate_live(
            self.ctx.db, uid, self.ctx.store.current(), mindset=self.mode()
        )

    def _store_debrief_setup(self, uid: int) -> None:
        if self.ctx.db is None:
            return
        self.setup_advisor.store_debrief(
            self.ctx.db, uid, self.ctx.store.current(), mindset=self.mode()
        )

    def _update_setup_stop_wing(self) -> None:
        self.setup_advisor.update_stop_wing(self.ctx.pit_plan.plan in _STOP_PLANS)

    def _synthetic_session(self, uid: int | None = None) -> bool:
        return self.ctx.synthetic_session(uid)

    def _upsert_session(self, uid: int) -> None:
        if self.ctx.db is None:
            return
        self._session_upserted = uid
        snap = self.ctx.state.snapshot(self.ctx.clock.now())
        self.ctx.db.upsert_session(
            uid,
            track_id=snap.track_id,
            session_type=snap.session_type,
            started_at=(
                self.session_origin_started_at
                if self.session_origin_started_at is not None
                else time.time()
            ),
            config_hash=self.ctx.store.hash,
            weather=snap.weather,
            game_mode=snap.game_mode,
            weekend_link=snap.weekend_link,
            weekend_structure=snap.weekend_structure,
            calls_mode=(
                "off"
                if self.ctx.store.current().policy.quiet
                or not self.ctx.store.current().speech.enabled
                else "on"
            ),
            parc_ferme=snap.parc_ferme if snap.parc_ferme >= 0 else None,
            synthetic=self._synthetic_session(uid),
        )
        self._parc_ferme_written = snap.parc_ferme if snap.parc_ferme >= 0 else None
        self._weekend_structure_written = tuple(snap.weekend_structure)

    def _write_laps(self) -> None:
        if self.ctx.state.session_uid is None:
            return
        uid = self.ctx.state.session_uid
        if self.ctx.db is None:
            new_laps = self.ctx.state.laps[self._laps_written :]
            self._laps_written = len(self.ctx.state.laps)
            for lap in new_laps:
                self._on_player_lap(uid, lap)
            return
        if self.ctx.recovering:
            # Rebuilt laps are already committed; don't insert or fold twice.
            self.ctx.state.rival_laps.clear()
            self.ctx.state.setup_changes.clear()
            self._laps_written = len(self.ctx.state.laps)
            return
        parc_ferme = self.ctx.state.parc_ferme_rules
        if parc_ferme >= 0 and parc_ferme != self._parc_ferme_written:
            self.ctx.db.set_session_parc_ferme(uid, parc_ferme)
            self._parc_ferme_written = parc_ferme
        structure = tuple(self.ctx.state.weekend_structure)
        if structure and structure != self._weekend_structure_written:
            self.ctx.db.set_weekend_structure(uid, structure)
            self._weekend_structure_written = structure
        if self.ctx.state.setup_rewind_t is not None:
            self.ctx.db.delete_setup_changes_after(uid, self.ctx.state.setup_rewind_t)
            self.ctx.state.setup_rewind_t = None
        setup_changed = bool(self.ctx.state.setup_changes)
        for change in self.ctx.state.setup_changes:
            to_id = self.ctx.db.setup_state_id(change.to_hash, change.fields)
            from_id = self.ctx.db.setup_state_id(change.from_hash) if change.from_hash else None
            self.ctx.db.insert_setup_change(
                uid,
                change.lap_num,
                change.session_time,
                from_id,
                to_id,
            )
        self.ctx.state.setup_changes.clear()
        for car_idx, lap in self.ctx.state.rival_laps:
            self.ctx.db.insert_lap(uid, car_idx, lap)
        self.ctx.state.rival_laps.clear()
        new_laps = self.ctx.state.laps[self._laps_written :]
        for lap in new_laps:
            self.ctx.db.insert_lap(
                uid,
                0,
                lap,
                setup_state_id=self.ctx.db.setup_state_id(lap.setup_hash)
                if lap.setup_hash
                else None,
            )
        self._laps_written = len(self.ctx.state.laps)
        if new_laps and self.ctx.state.total_laps > 0:
            self.ctx.db.set_session_total_laps(uid, self.ctx.state.total_laps)
        for lap in new_laps:
            self._on_player_lap(uid, lap)
        if new_laps or setup_changed:
            if uid != self.setup_advisor.session_uid:
                self.setup_advisor.reset(uid)
            if new_laps or session_kind(self.ctx.state.session_type) == "race":
                self._evaluate_live_setup(uid)
            else:
                # Garage advice was built on the setup the driver just changed.
                # Drop it until a lap on the new setup exists.
                self.setup_advisor.clear()

    def _on_player_lap(self, uid: int, lap: LapSummary) -> None:
        """M3 persistence hooks (docs/18): refit the stint, measure pit loss
        when an out-lap completes, fold fuel-burn deltas."""
        self.model._note_session_fuel(lap)
        db = self.ctx.db
        settings = self.ctx.store.current()
        track_id = self.ctx.state.track_id
        if db is None:
            rows_by_lap = {
                row.lap_num: row
                for row in (
                    lap_row_from_summary(uid, 0, summary) for summary in self.ctx.state.laps
                )
            }
            rows = sorted(rows_by_lap.values(), key=lambda row: row.lap_num)
            in_memory_tail = self.model._stint_rows(rows)
            if in_memory_tail:
                self.model._fit_current_stint(in_memory_tail, track_id, settings, None)
            return
        th = settings.thresholds
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
            prev = self.model._stint_rows(all_laps[: len(all_laps) - len(tail)])
            key = (uid, prev[0].lap_num) if prev else (uid, tail[0].lap_num)
            if key not in self._folded_stints:
                self._folded_stints.add(key)
                if prev:
                    prior = self.model._deg_prior(track_id, prev[0].compound, settings)
                    fit = fit_stint(
                        prev,
                        prior,
                        min_laps=int(self._th("deg_min_laps", 3)),
                        fuel_coeff_fixed=None,
                        deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                        deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
                        thermal_hi_c=self.model._thermal_hi_c(track_id, prev[0].compound, settings),
                    )
                    self.model._fold_fit(track_id, prev[0].compound, fit)
                    self.model._weekend_prior_cache.pop((uid, prev[0].compound), None)

        if tail:
            compound = tail[0].compound
            fuel_name = self.model._learned_name(track_id, compound, "fuel_ms_per_lap")
            fuel_param = db.get_param(track_id, compound, fuel_name)
            fuel_fixed = (
                fuel_param.value
                if fuel_param is not None and fuel_param.weight >= self._th("prior_min_weight", 2)
                else None
            )
            fit = self.model._fit_current_stint(tail, track_id, settings, fuel_fixed)
            persist_fit = fit
            if fit.deg2_ms_per_lap2 > 0:
                prior = self.model._deg_prior(track_id, compound, settings)
                persist_fit = fit_stint(
                    tail,
                    prior,
                    min_laps=int(self._th("deg_min_laps", 3)),
                    fuel_coeff_fixed=fuel_fixed,
                    deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                    deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
                    thermal_hi_c=self.model._thermal_hi_c(track_id, compound, settings),
                )
            db.upsert_stint(
                uid,
                0,
                compound,
                tail[0].lap_num,
                tail[-1].lap_num,
                persist_fit,
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
                        lane_ms=self.ctx.state.pit_lane_time_ms,
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
            if self.model._fuel_last_kg is not None:
                delta = self.model._fuel_last_kg - lap.fuel_kg
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
            self.model._fuel_last_kg = lap.fuel_kg
        elif lap.fuel_kg > 0:
            self.model._fuel_last_kg = lap.fuel_kg

        self.ctx.pit_loss_prior = current_pit_loss(
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
        if self.ctx.db is None:
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
        if pending.neutralised == 0 and pit.in_lap_ms - pit.ref_pace_ms > self._th(
            "pit_inlap_slow_max_ms", 10_000
        ):
            return
        self.ctx.db.insert_pit_event(
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
            self.ctx.db.fold_param(
                pending.track_id,
                0,
                f"pit_loss_{suffix}_ms",
                float(pit.loss_ms),
                param_weight_cap=self._th("param_weight_cap", 50),
            )

    def fold_open_stint(self) -> None:
        """Persist the current tail stint when a session closes."""
        if self.ctx.db is None or self.ctx.state.session_uid is None:
            self._pending_pit_loss = None
            return
        self._write_laps()
        uid = self.ctx.state.session_uid
        rows = self.ctx.db.laps_for(uid, 0)
        self._flush_pending_pit_loss(uid, rows, force=True)
        stint = self.model._stint_rows(rows)
        if not stint:
            return
        key = (uid, stint[0].lap_num)
        if key in self._folded_stints:
            return
        self._folded_stints.add(key)
        settings = self.ctx.store.current()
        prior = self.model._deg_prior(self.ctx.state.track_id, stint[0].compound, settings)
        fit = fit_stint(
            stint,
            prior,
            min_laps=int(self._th("deg_min_laps", 3)),
            fuel_coeff_fixed=None,
            deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
            deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
            thermal_hi_c=self.model._thermal_hi_c(
                self.ctx.state.track_id, stint[0].compound, settings
            ),
        )
        self.ctx.db.upsert_stint(
            uid,
            0,
            stint[0].compound,
            stint[0].lap_num,
            stint[-1].lap_num,
            fit,
            setup_state_id=majority_state(stint),
        )
        self.model._weekend_prior_cache.pop((uid, stint[0].compound), None)
        self.model._fold_fit(self.ctx.state.track_id, stint[0].compound, fit)

    def fold_session_end(self) -> None:
        uid = self.ctx.state.session_uid
        if self.ctx.db is None or uid is None:
            return
        self.fold_open_stint()
        self.model._fold_driver(uid)

    def _th(self, name: str, default: float) -> float:
        return self.ctx.th(name, default)

    def _plan_damage(self, snap: Snapshot, fit: DegFit) -> tuple[str, float, float]:
        if snap.num_pit_stops != self._damage_stops:
            self._damage_pace_ms = 0.0
            self._damage_stops = snap.num_pit_stops
        if snap.race_phase not in ("racing", "sc", "vsc"):
            return "", 0.0, 0.0
        wing_pct = max(snap.damage.front_left_wing, snap.damage.front_right_wing)
        wing = wing_pct >= self._th("plan_damage_wing_pct", 15)
        tyre = bool(snap.puncture_corner)
        last = self.ctx.state.laps[-1] if self.ctx.state.laps else None
        measured = None
        if (
            last is not None
            and usable_laps(
                [lap_row_from_summary(snap.session_uid, 0, last)],
                self.model._thermal_hi_c(snap.track_id, last.compound, self.ctx.store.current()),
            )
            and last.lap_num > 1
            and last.sc_status == 0
            and last.lap_num == snap.lap_num - 1
            and snap.tyre_age_laps > 0
        ):
            age = last.tyre_age_laps
            predicted = fit.base_ms + fit.deg_ms_per_lap * age + fit.deg2_ms_per_lap2 * age**2
            measured = max(0.0, last.lap_time_ms - predicted)
        pace = measured is not None and measured >= self._th("plan_damage_pace_ms", 1500)
        positions = (
            0 < snap.lap_num <= self._th("plan_rebuild_max_lap", 2)
            and snap.grid_position > 0
            and snap.position > 0
            and snap.position - snap.grid_position >= self._th("plan_rebuild_positions_lost", 3)
        )
        cause = "damage" if wing or tyre else "pace" if pace else "positions" if positions else ""
        penalty = 0.0
        if wing or tyre:
            penalty = max(
                measured or 0.0,
                wing_pct * self._th("damage_pace_ms_per_wing_pct", 20) if wing else 0.0,
                self._th("damage_tyre_pace_ms", 1500) if tyre and measured is None else 0.0,
            )
        elif pace:
            penalty = measured or 0.0
        self._damage_pace_ms = max(self._damage_pace_ms, penalty)
        penalty = self._damage_pace_ms
        if not cause and penalty > 0:
            cause = "pace"
        return cause, penalty, self._th("pit_wing_change_s", 5.0) if wing else 0.0

    def _plan(self, snap: Snapshot) -> Snapshot:
        """Run the pit-window optimiser on the fresh snapshot and fold the
        plan into both the snapshot and the next ModelView."""
        fit = self.ctx.deg_fit
        if not snap.race_phase or self.ctx.fresh_prior is None:
            return snap
        if fit is None:
            if self._plan_damage(snap, self.ctx.fresh_prior)[1] <= 0:
                return self._plans(snap, self.ctx.fresh_prior, NO_PLAN)
            fit = self.ctx.fresh_prior
        fit = planning_fit(fit, self.ctx.fresh_prior, self._th("deg_rmse_bad_ms", 800))
        settings = self.ctx.store.current()
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
        behind, behind_gap_s = _true_behind_view(snap)
        raw_mode = resolve_mindset(settings.mindsets, self.mindset)
        drv = self._driver_scale(settings, raw_mode)
        mode = self.mode()
        used = frozenset(c for c in self._used_compounds | {snap.tyre_visual} if c)
        _, damage_pace_ms, wing_loss_s = self._plan_damage(snap, fit)
        vsc_ending = snap.race_phase == "vsc" and snap.sc_ending
        pit_loss_s = snap.pit_loss_s + wing_loss_s
        sc_status = snap.safety_car_status if snap.race_phase in ("sc", "vsc") else 0
        if vsc_ending:
            pit_loss_s = (self.ctx.green_pit_loss_s or snap.pit_loss_s) + wing_loss_s
            sc_status = 0
        plan = optimise(
            lap_num=snap.lap_num,
            laps_remaining=snap.laps_remaining,
            tyre_age=snap.tyre_age_laps,
            wear_mean=snap.wear_mean_pct,
            fit=fit,
            fresh=self.ctx.fresh_prior,
            laps_of_pace=snap.laps_of_pace,
            pit_loss_s=pit_loss_s,
            damage_pace_ms=damage_pace_ms,
            pit_loss_source=snap.pit_loss_source,
            green_pit_loss_s=self.ctx.green_pit_loss_s,
            sc_status=sc_status,
            rival_ahead=ahead,
            rival_behind=behind,
            gap_ahead_s=snap.gap_ahead_s,
            gap_behind_s=snap.gap_behind_s,
            rival_behind_gap_s=behind_gap_s,
            pit_exit_clean=snap.pit_exit_clean,
            restricted=snap.rival_data_restricted,
            stop_owed=self.plan_tracker.state.stops_left > 0,
            stop_mandatory=compound_stop_owed(used, self._plan_thresholds(snap)),
            driver=drv,
            mode=mode,
            th=settings.thresholds,
            teammate_pitting=snap.teammate_stacking,
            field=snap.field_cars,
        )
        if snap.safety_car_status == 0:
            session_uid = self.ctx.state.session_uid
            raw_cadence = mode.get("mc_every_n_laps", 1)
            mc_every = max(1, int(raw_cadence) if isinstance(raw_cadence, int | float) else 1)
            raw_sims = mode.get("plan_mc_sims", settings.thresholds.get("plan_mc_sims", 500))
            mc_sims = int(raw_sims) if isinstance(raw_sims, int | float) else 500
            lap_slot = snap.lap_num if mc_every == 1 else (snap.lap_num - 1) // mc_every
            age_slot = snap.tyre_age_laps if mc_every == 1 else snap.lap_num - snap.tyre_age_laps
            key = (
                session_uid,
                lap_slot,
                plan.lap,
                age_slot,
                threshold(mode, "pit_gain_min_s", 1.0),
                damage_pace_ms,
                wing_loss_s,
                plan.traffic_penalties_s,
                plan.plan,
                plan.rival_idx,
                snap.teammate_stacking,
                mc_sims,
            )
            if key not in self._plan_mc_cache:
                uid = session_uid if session_uid is not None else 0
                rng = np.random.default_rng(
                    [uid & 0xFFFFFFFF, (uid >> 32) & 0xFFFFFFFF, snap.lap_num]
                )
                self._plan_mc_cache[key] = plan_agreement(
                    lap_num=snap.lap_num,
                    laps_remaining=snap.laps_remaining,
                    tyre_age=snap.tyre_age_laps,
                    fit=fit,
                    fresh=self.ctx.fresh_prior,
                    cur_cliff_age=snap.tyre_age_laps + snap.laps_of_pace,
                    pit_loss_s=snap.pit_loss_s + wing_loss_s,
                    damage_pace_ms=damage_pace_ms,
                    stack_s=(
                        threshold(settings.thresholds, "double_stack_loss_s", 4.0)
                        if snap.teammate_stacking
                        else 0.0
                    ),
                    pit_loss_samples_s=self.ctx.green_pit_samples_s,
                    sc_pit_loss_s=self.ctx.sc_pit_loss_s,
                    plan_lap=plan.lap,
                    mode=mode,
                    th=settings.thresholds,
                    rng=rng,
                    plan_name=plan.plan,
                    cover_gap_s=behind_gap_s,
                    traffic_penalties_s=plan.traffic_penalties_s,
                )
            plan = scale_plan(plan, self._plan_mc_cache[key], lap_num=snap.lap_num)
        plan, self._held_plan = _hold_cover_plan(
            self._held_plan, plan, snap, self._th("pit_min_laps_left", 2)
        )
        self.ctx.pit_plan = plan
        self.ctx.state.set_model(
            dataclasses.replace(
                self.ctx.state.model,
                pit_plan=plan.plan,
                pit_plan_lap=plan.lap,
                pit_plan_gain_s=plan.gain_s,
                pit_plan_confidence=plan.confidence,
                pit_plan_risk=plan.risk,
                pit_plan_risky=plan.risky,
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
            pit_plan_risky=plan.risky,
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
        settings = self.ctx.store.current()
        current = snap.tyre_visual
        names = {16: "soft", 17: "medium", 18: "hard"}
        out: list[CompoundModel] = []
        for v in DRY:
            actual = next((s.actual for s in snap.tyre_sets if s.visual == v and s.actual), 0)
            deg = cur.deg_ms_per_lap * (
                self._th(f"plan_deg_factor_{names[v]}", 1.0)
                / self._th(f"plan_deg_factor_{names.get(current, 'medium')}", 1.0)
            )
            deg_sd = cur.deg_posterior_sd_ms_per_lap * (
                self._th(f"plan_deg_factor_{names[v]}", 1.0)
                / self._th(f"plan_deg_factor_{names.get(current, 'medium')}", 1.0)
            )
            if actual:
                prior = self.model._compound_priors.get(actual)
                if prior is None:
                    prior = self.model._deg_prior(snap.track_id, actual, settings)
                    self.model._compound_priors[actual] = prior
                if prior.source in ("learned", "overlay"):
                    deg = prior.deg_ms_per_lap
                    deg_sd = 0.0
            off = self._th(f"plan_pace_{names[v]}_ms", 0.0)
            off_cur = self._th(f"plan_pace_{names[current]}_ms", 0.0) if current in names else 0.0
            sets = (
                sum(1 for s in snap.tyre_sets if s.visual == v and s.available and not s.fitted)
                if snap.tyre_sets
                else -1
            )
            out.append(
                CompoundModel(
                    v,
                    cur.base_ms + off - off_cur,
                    deg,
                    sets,
                    deg_posterior_sd_ms_per_lap=deg_sd,
                )
            )
        return tuple(out)

    def _plans(self, snap: Snapshot, cur: DegFit, pit: PitPlan) -> Snapshot:
        """Named strategy plans: recompute on each lap / stop / SC change,
        persist set/switch/off events, fold scalars into snapshot + model."""
        tracker = self.plan_tracker
        if snap.pit_status != 0 and self._prev_pit_status == 0:
            previous = view_fields(tracker.state, snap.lap_num)
            self._stop_expect = (
                snap.active_plan or previous.active_plan,
                (snap.plan_next_compound or previous.plan_next_compound).upper(),
                snap.num_pit_stops,
            )
            self._stop_check = None
        elif snap.pit_status == 0 and self._prev_pit_status > 0:
            expect = self._stop_expect
            # A drive-through or stop-go also passes the pit lane; only a real stop
            # raises num_pit_stops (the game counts it in the box, before pit exit).
            if expect is not None and expect[1] and snap.num_pit_stops > expect[2]:
                self._stop_check = (expect[0], expect[1], snap.lap_num, snap.session_time)
            self._stop_expect = None
        if self._stop_check is not None and snap.lap_num != self._stop_check[2]:
            self._stop_check = None
        self._prev_pit_status = snap.pit_status
        snap = dataclasses.replace(
            snap,
            stop_plan=self._stop_check[0] if self._stop_check is not None else "",
            stop_expected_compound=self._stop_check[1] if self._stop_check is not None else "",
        )
        phase = snap.race_phase
        if phase == "out_lap" and self._prev_race_phase != "out_lap":
            self._stops_done += 1
        self._prev_race_phase = phase
        if phase == "red_flag":
            self._red_restart = True  # rebuild the plan from the restart tyres
            self._plan_key = None
        if phase in ("racing", "sc", "vsc") and snap.tyre_visual and snap.laps_remaining > 0:
            self._used_compounds.add(snap.tyre_visual)
            neutral = phase in ("sc", "vsc") and not (phase == "vsc" and snap.sc_ending)
            cheap = pit.plan == "cheap_stop"
            invalidate, damage_pace_ms, wing_loss_s = self._plan_damage(snap, cur)
            key = (
                snap.lap_num,
                self._stops_done,
                phase if neutral else "",
                cheap,
                snap.tyre_visual,
                self.mindset,
                invalidate,
                round(damage_pace_ms / 100),
                round(wing_loss_s, 1),
                snap.is_sprint,
            )
            if key != self._plan_key:
                if self._plan_key is not None and self._plan_key[-1] != snap.is_sprint:
                    tracker.reset()  # the weekend format arrived: a fresh plan, not a switch
                reseed = self._plan_key is not None and self._plan_key[5] != self.mindset
                self._plan_key = key
                lop = snap.laps_of_pace
                cliff = self._th("tyre_cliff_ms", 1500)
                cur_cliff_age = (
                    snap.tyre_age_laps + lop if math.isfinite(lop) else cliff_age(cur, cliff)
                )
                tracker.update(
                    PlanInputs(
                        lap_num=snap.lap_num,
                        laps_remaining=snap.laps_remaining,
                        tyre_age=snap.tyre_age_laps,
                        current=snap.tyre_visual,
                        cur=cur,
                        cur_cliff_age=cur_cliff_age,
                        compounds=self._compound_models(snap, cur),
                        used=frozenset(self._used_compounds),
                        green_loss_s=self.ctx.green_pit_loss_s or snap.pit_loss_s,
                        sc_loss_s=self.ctx.sc_pit_loss_s or snap.pit_loss_s,
                        wing_change_s=wing_loss_s,
                        damage_pace_ms=damage_pace_ms,
                    ),
                    self._plan_thresholds(snap),
                    stops_done=self._stops_done,
                    neutralised=neutral,
                    cheap_stop=cheap,
                    reseed=reseed,
                    invalidate=invalidate,
                    restart=self._red_restart,
                )
                self._red_restart = False
                for ev in tracker.drain():
                    self._persist_plan_event(snap, ev, tracker.state)
        st: PlanState = tracker.state
        if not st.active:
            return snap
        fields = view_fields(st, snap.lap_num)
        self.ctx.state.set_model(_with_plan(self.ctx.state.model, fields))
        return _with_plan(snap, fields)

    def _persist_plan_event(self, snap: Snapshot, ev: PlanEvent, st: PlanState) -> None:
        plans = [
            {
                "id": p.id,
                "sequence": p.sequence,
                "window": list(p.window),
                "delta_s": p.delta_s,
                "delta_sd_s": p.delta_sd_s,
                "switch_prob": p.switch_prob,
            }
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
            "off_prob": st.off_prob,
        }
        self.ctx.dispatcher.log.write(record)
        if self.ctx.db is not None and self.ctx.state.session_uid is not None:
            self.ctx.db.insert_plan_event(self.ctx.state.session_uid, record)

    def _battle(self, snap: Snapshot) -> Snapshot:
        """Battle mode + attack/defend episodes; episode outcomes fold into
        the track's pass model and are written to the decision log."""
        if snap.session_kind != "race" or snap.race_phase != "racing":
            return snap
        attack = self.mode().get("attack_window_s", 1.0)
        drv = self._driver_scale()
        own = snap.predicted_lap_ms or int(snap.base_pace_ms)
        inp = BattleInputs(
            now=snap.now,
            lap_num=snap.lap_num,
            position=snap.position,
            laps_remaining=snap.laps_remaining,
            ahead_idx=snap.rival_ahead_idx,
            behind_idx=-1 if snap.rival_behind_in_pit_lane else snap.rival_behind_idx,
            gap_ahead_s=snap.gap_ahead_s,
            gap_behind_s=math.inf if snap.rival_behind_in_pit_lane else snap.gap_behind_s,
            trend_ahead_s=snap.gap_trend_ahead_s,
            trend_behind_s=snap.gap_trend_behind_s,
            own_pace_ms=own,
            ahead_pace_ms=snap.rival_ahead_pace_ms,
            behind_pace_ms=snap.rival_behind_pace_ms,
            own_age=snap.tyre_age_laps,
            ahead_age=snap.rival_ahead_age,
            behind_age=snap.rival_behind_age,
            ahead_age_known=snap.rival_ahead_age_known,
            behind_age_known=snap.rival_behind_age_known,
            overtake_active=bool(snap.overtake_active)
            if snap.regulations_2026
            else snap.drs_available,
            attack_gap_s=float(attack) if isinstance(attack, int | float) else 1.0,
            positions=tuple(c.car_position for c in snap.cars),
            pitting=frozenset(i for i, c in enumerate(snap.cars) if c.pit_status != 0),
            regulations_2026=snap.regulations_2026,
            closing_sd_s=snap.gap_closing_sd_ahead_s,
            pass_prob_min=drv.pass_prob_min,
            driver=drv,
        )
        tracker = self.battle_tracker
        th = self.ctx.store.current().thresholds
        b: Battle = tracker.update(inp, th, self.battle_rates(snap.track_id))
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
            battle_tyre_known_ahead=b.tyre_known_ahead,
            battle_tyre_known_behind=b.tyre_known_behind,
            battle_behind_changed=b.behind_changed,
            battle_pass_prob=b.pass_prob,
            battle_attack_pass_prob=b.attack_pass_prob,
            battle_catch_prob=b.catch_prob,
            battle_chase_prob=b.chase_prob,
            battle_hold_prob=b.hold_prob,
            battle_result=b.result,
            battle_result_recent=b.result_recent,
            battle_result_name=name,
            battle_swaps=tracker.swap_count(inp.now, th),
            battle_pace_ahead=_rival_pace(snap, snap.rival_ahead_idx, -b.closing_ahead_s),
            battle_pace_behind=_rival_pace(snap, snap.rival_behind_idx, b.closing_behind_s),
        )

    def _persist_episode(self, snap: Snapshot, ep: Episode) -> None:
        self.ctx.dispatcher.log.write(
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
        if self.ctx.db is None or snap.track_id < 0 or self._synthetic_session():
            return
        name = HOLD if ep.kind == "defend" else PASS_OT if ep.overtake else PASS_NO_OT
        cap = self._th("param_weight_cap", 50.0)
        self.ctx.db.fold_param(
            snap.track_id,
            PASS_COMPOUND,
            name,
            1.0 if ep.success else 0.0,
            1.0,
            param_weight_cap=cap,
        )
        self.model._battle_rates = None

    def _end_session(self, uid: int, now: float) -> None:
        assert self.ctx.db is not None
        self.fold_open_stint()
        self.model._fold_driver(uid)
        self._store_debrief_setup(uid)
        self._session_ended_written = True
        self.ctx.db.end_session(uid, now)
        grade_and_store(self.ctx.db, uid, self.ctx.store.current().thresholds)
        self.ctx.db.mark_graded(uid)

    def tick(self, now: float) -> list[Call]:
        self.model._sync_run_fuel()
        self.ctx.store.poll(now)
        self.setup_advisor.refresh_rules(self.ctx.store.current().setup_rules)
        if self.ctx.state.track_id != self._track_loaded:
            self._track_loaded = self.ctx.state.track_id
            self.ctx.store.set_track(
                self.ctx.state.track_id if self.ctx.state.track_id >= 0 else None
            )
            self.model._refresh_driver_rating(self.ctx.state.track_id)
        if (
            self.ctx.db is not None
            and self.ctx.state.session_uid is not None
            and self.ctx.state.track_id >= 0
            and self.ctx.state.session_type > 0
            and self._session_upserted != self.ctx.state.session_uid
        ):
            self._upsert_session(self.ctx.state.session_uid)
        self._write_laps()
        self.model._update_model()
        snapshot = self._battle(self._plan(self.ctx.state.snapshot(now)))
        self._update_setup_stop_wing()
        snapshot = dataclasses.replace(snapshot, **self.setup_advisor.snapshot_fields())
        uid = self.ctx.state.session_uid
        if (
            snapshot.session_ended
            and not self._session_ended_written
            and self.ctx.db is not None
            and uid is not None
        ):
            self._end_session(uid, now)
            if self.ctx.on_session_end is not None:
                self.ctx.on_session_end(uid)
        press = self.detector.tick(now)
        if press is not None:
            self._press_queue.append(press)
        balance = self.driver_balance(snapshot.lap_num)
        if balance != snapshot.driver_balance:
            snapshot = dataclasses.replace(snapshot, driver_balance=balance)
        self.ctx.last_snapshot = snapshot
        for p in self._press_queue:
            if self._menu_press(p, snapshot):
                continue
            if p.kind == "mindset":
                self.cycle_mindset(p.t)
            elif p.kind == "page":
                self.reset_page(p.t)
            else:
                self.ctx.dispatcher.on_press(p, snapshot)
        self._press_queue.clear()
        if self.ctx.menu.expired(self.ctx.store.current().menu, now):
            self._menu_close(now, snapshot, "timeout")
        self._voice_tick(now, snapshot)
        self._auto_page(snapshot, now)
        if self.ctx.state.last_recv_wall is not None:
            self.metrics.note_packet_to_snapshot(self.ctx.state.last_recv_wall, now)
        if snapshot.paused != self._paused:
            self.ctx.dispatcher.log.write(
                {"t": now, "outcome": "paused" if snapshot.paused else "resumed"}
            )
            if snapshot.paused:
                self.ctx.dispatcher.purge(reason="paused", now=now)
            self._paused = snapshot.paused
        if snapshot.paused:
            return []
        self._heartbeat(now)
        if self.ctx.recovering:
            if self.rule_engine is not None:
                # Arm/disarm edges on the rebuilt state so conditions already
                # true before the crash don't all fire on the first live tick.
                self.rule_engine.evaluate(snapshot)
            return []
        if self.rule_engine is not None:
            result = self.rule_engine.evaluate(snapshot)
            for suppressed in result.suppressed:
                if suppressed.reason not in {"expr_error", "clear_error"}:
                    continue
                key = (suppressed.rule.id, suppressed.reason)
                if key in self._rule_errors_logged:
                    continue
                self._rule_errors_logged.add(key)
                self.ctx.dispatcher.log.write(
                    {
                        "t": now,
                        "outcome": "rule_error",
                        "rule_id": None,
                        "text": f"{suppressed.rule.id} {suppressed.reason}: {suppressed.detail}",
                    }
                )
            self.ctx.dispatcher.submit(result.candidates, snapshot)
        return self.ctx.dispatcher.drain(now)

    def _heartbeat(self, now: float) -> None:
        period = self.ctx.store.current().engine.heartbeat_s
        if self.ctx.db is None or self.ctx.state.session_uid is None or period <= 0:
            return
        if self._last_heartbeat is not None and now - self._last_heartbeat < period:
            return
        self._last_heartbeat = now
        path = self.recording_path_source() if self.recording_path_source is not None else None
        self.ctx.db.write_heartbeat(
            self.ctx.state.session_uid,
            float(self.ctx.state.last_packet_t or 0.0),
            time.time(),
            str(path or ""),
            self.ctx.state.lap_num,
        )

    def recover(self, wall_now: float | None = None) -> str | None:
        """Mid-session restart: if the SQLite heartbeat is fresh and names a
        recording, replay that recording's tail through ingest (not the
        recorder, rules muted) so EMAs, laps and phase are rebuilt; the model
        reads its lap history from the committed SQLite rows. Returns a short
        description, or None when there is nothing to recover."""
        if self.ctx.db is None:
            return None
        hb = self.ctx.db.read_heartbeat()
        eng = self.ctx.store.current().engine
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
        self.ctx.recovering = True
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
            self.ctx.recovering = False
            self.ingest.recorder = recorder
            self.ctx.dispatcher.purge(reason="recovery", now=self.ctx.clock.now())
        if self.ctx.state.session_uid is not None and self.ctx.state.session_uid != hb.session_uid:
            return None
        self._session_upserted = self.ctx.state.session_uid
        self.ctx.dispatcher.log.write(
            {
                "t": self.ctx.clock.now(),
                "outcome": "recovered",
                "rule_id": None,
                "text": f"{path.name} tail {len(tail)} datagrams, lap {self.ctx.state.lap_num}",
            }
        )
        return f"recovered lap {self.ctx.state.lap_num} from {path.name} ({len(tail)} datagrams)"

    def rejoin_text(self) -> str:
        """Spoken after a successful recover()."""
        from pitwall.audio.speaker import REJOIN_OPENER

        st = self.ctx.state
        parts = [REJOIN_OPENER]
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
                self.tick(self.ctx.clock.now())
            except Exception:  # noqa: BLE001 - watchdog: one bad tick must not end a race
                self.ctx.dispatcher.log.write(
                    {
                        "t": self.ctx.clock.now(),
                        "outcome": "tick_error",
                        "rule_id": None,
                        "text": traceback.format_exc(limit=3),
                    }
                )
            await self.ctx.clock.sleep(period)


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
        log_path = storage_path(settings.recording.directory) / "decisions.jsonl"
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
        budget_override=call_budget(mode.get("call_budget_per_lap")),
        input=settings.input,
    )
    engine = Engine(
        store,
        clock,
        ingest,
        state,
        rule_engine,
        dispatcher,
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
