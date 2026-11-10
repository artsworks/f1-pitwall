"""Engine: snapshot -> evaluate -> submit, driven live or by replay.

Replay ticks whenever the record clock crosses a tick boundary, so a 10x or
max-speed replay makes identical decisions to 1x (determinism per docs/07).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

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
from pitwall.engine.battle import BattleRunner
from pitwall.engine.context import EngineContext
from pitwall.engine.model import SessionModel
from pitwall.engine.persistence import SessionWriter
from pitwall.engine.planning import _STOP_PLANS, Planner
from pitwall.ingest import Ingest
from pitwall.input.menu import DriverMenu, ReplyPicker, answer
from pitwall.input.press import Press, PressDetector
from pitwall.learning.tune import load_cooldown_mults
from pitwall.metrics import Metrics
from pitwall.model.budget import EnergyBudget, FuelBudget
from pitwall.model.deg import (
    DegFit,
    Prior,
)
from pitwall.model.driver import (
    DriverScale,
    advice_scale,
)
from pitwall.rules.engine import STALENESS_DEFAULT_S, RuleEngine
from pitwall.rules.expr import expr_names, namespace_data
from pitwall.setup.advisor import SetupAdvisor
from pitwall.state.session import SessionState, Snapshot
from pitwall.strategy.battle import (
    BattleRates,
)
from pitwall.strategy.pitwindow import NO_PLAN, PitPlan
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


_MENU_CLIENT_OPS = {
    "up": "menu_up",
    "down": "menu_down",
    "confirm": "menu_confirm",
    "close": "menu_close",
}


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
        self.model = SessionModel(self.ctx, self._apply_mode, lambda: self.writer._laps_written)
        self._apply_mode()
        # Crash recovery (docs/18): heartbeat to SQLite; on restart replay the
        # recording tail with the rules muted.
        self.ctx.recovering = False
        self._alive_t = -math.inf
        self._rule_errors_logged: set[tuple[str, str]] = set()
        self.ctx.dispatcher.tuned_cooldown = load_cooldown_mults(self.ctx.db)
        self._track_loaded: int | None = None
        # M3 model outputs, exposed on the engine until H2 moves them into
        # the snapshot.
        self.ctx.deg_fit = None
        self.ctx.pit_loss_prior = None
        self.ctx.pit_plan = NO_PLAN
        self.ctx.fresh_prior = None
        self.ctx.green_pit_loss_s = 0.0
        self.ctx.green_pit_samples_s = []
        self.ctx.sc_pit_loss_s = 0.0
        self.ctx.fuel_budget = None
        self.ctx.energy_budget = None
        self.ctx.energy_prev_lap = None
        self.ctx.stint_fuel_ref = 0.0
        self.ctx.stint_laps = []

        self.writer = SessionWriter(
            self.ctx,
            self.model,
            ingest,
            self.setup_advisor,
            self.tick,
            self._evaluate_live_setup,
            self._store_debrief_setup,
        )
        self.planner = Planner(
            self.ctx,
            self.model,
            self._driver_scale,
            self._plan_thresholds,
            lambda: self.mindset,
        )
        self.battle = BattleRunner(self.ctx, self.model, self._driver_scale)

        def _on_rewind(t: float) -> None:
            dispatcher.purge(reason="flashback", now=t)
            self.model.rewind(t)
            self.planner.rewind(t)

        state.rewind_listeners.append(_on_rewind)
        state.session_listeners.append(self._on_new_session)
        state.session_end_listeners.append(self.fold_session_end)

    def fold_open_stint(self) -> None:
        return self.writer.fold_open_stint()

    def fold_session_end(self) -> None:
        return self.writer.fold_session_end()

    def recover(self, wall_now: float | None = None) -> str | None:
        return self.writer.recover(wall_now)

    def rejoin_text(self) -> str:
        return self.writer.rejoin_text()

    @property
    def recording_path_source(self) -> Any:
        return self.writer.recording_path_source

    @recording_path_source.setter
    def recording_path_source(self, value: Any) -> None:
        self.writer.recording_path_source = value

    @property
    def alive_path(self) -> Path | None:
        return self.writer.alive_path

    @alive_path.setter
    def alive_path(self, value: Path | None) -> None:
        self.writer.alive_path = value

    @property
    def session_origin_started_at(self) -> float | None:
        return self.writer.session_origin_started_at

    @session_origin_started_at.setter
    def session_origin_started_at(self, value: float | None) -> None:
        self.writer.session_origin_started_at = value

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
        self.writer.flush_previous_session()
        self.ctx.dispatcher.reset_session(uid)
        self.planner._held_plan = None
        self._rule_errors_logged.clear()
        self.model._reset_energy_lap_tracking()
        self.ctx.menu.close()
        self._voice_close(self.ctx.clock.now(), "session")
        self._menu_replies.reset()
        self.ctx.opinions.clear()
        self.writer.reset()
        self.model.reset()
        self.ctx.deg_fit = None
        self.planner.reset()
        self.battle.reset()
        self.ctx.stint_fuel_ref = 0.0
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
            and self.writer._session_upserted != self.ctx.state.session_uid
        ):
            self.writer._upsert_session(self.ctx.state.session_uid)
        self.writer._write_laps()
        self.model._update_model()
        snapshot = self.battle._battle(self.planner._plan(self.ctx.state.snapshot(now)))
        self._update_setup_stop_wing()
        snapshot = dataclasses.replace(snapshot, **self.setup_advisor.snapshot_fields())
        uid = self.ctx.state.session_uid
        if (
            snapshot.session_ended
            and not self.writer._session_ended_written
            and self.ctx.db is not None
            and uid is not None
        ):
            self.writer._end_session(uid, now)
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
        self.writer._heartbeat(now)
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

    async def run_live(self) -> None:
        """Tick at tick_hz forever; dispatch drains on its own loop. The
        watchdog keeps the loop alive through a failing tick (logged)."""
        period = self.tick_period
        while True:
            if self.writer.alive_path is not None and time.monotonic() - self._alive_t >= 1.0:
                self._alive_t = time.monotonic()
                with contextlib.suppress(OSError):
                    self.writer.alive_path.touch()
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
