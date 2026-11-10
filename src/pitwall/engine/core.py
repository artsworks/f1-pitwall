"""Engine: snapshot -> evaluate -> submit, driven live or by replay.

Replay ticks whenever the record clock crosses a tick boundary, so a 10x or
max-speed replay makes identical decisions to 1x (determinism per docs/07).
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, CallSink, Dispatcher, LogSink
from pitwall.clock import Clock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import (
    InputSettings,
    call_budget,
)
from pitwall.config.paths import storage_path
from pitwall.engine.battle import BattleRunner
from pitwall.engine.context import EngineContext
from pitwall.engine.controls import Controls
from pitwall.engine.menu import MenuController
from pitwall.engine.model import SessionModel
from pitwall.engine.persistence import SessionWriter
from pitwall.engine.planning import _STOP_PLANS, Planner
from pitwall.engine.voice import VoiceController
from pitwall.ingest import Ingest
from pitwall.input.menu import DriverMenu
from pitwall.learning.tune import load_cooldown_mults
from pitwall.metrics import Metrics
from pitwall.model.budget import EnergyBudget, FuelBudget
from pitwall.model.deg import (
    DegFit,
    Prior,
)
from pitwall.rules.engine import RuleEngine
from pitwall.setup.advisor import SetupAdvisor
from pitwall.state.session import SessionState, Snapshot
from pitwall.strategy.battle import (
    BattleRates,
)
from pitwall.strategy.pitwindow import PitPlan
from pitwall.voice.channel import VoiceChannel
from pitwall.voice.worker import VoiceWorker


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
        settings = store.current()
        self.setup_advisor = SetupAdvisor(state, settings.setup_rules)
        self.ingest = ingest
        self.ctx.synthetic_source = synthetic_source
        self.rule_engine = rule_engine
        self.ctx.dispatcher.on_response = self._on_dispatcher_response
        self.metrics = dispatcher.metrics
        self.speaker_name = "null"
        self.recording_desc = "off"
        self._paused = False
        self.ctx.db = dispatcher.log.db
        self.ctx.driver_db = self.ctx.db
        self.voice_controller = VoiceController(self.ctx)
        self.controls = Controls(self.ctx, rule_engine, self.voice_controller)
        self.menu_controller = MenuController(
            self.ctx,
            self.controls,
            self.voice_controller,
            budget_label,
        )
        self.voice_controller.menu = self.menu_controller
        self.model = SessionModel(
            self.ctx, self.controls._apply_mode, lambda: self.writer._laps_written
        )
        self.controls._apply_mode()
        self.ctx.dispatcher.tuned_cooldown = load_cooldown_mults(self.ctx.db)
        self._alive_t = -math.inf
        self._rule_errors_logged: set[tuple[str, str]] = set()
        self._track_loaded: int | None = None

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
            self.controls._driver_scale,
            self.controls._plan_thresholds,
            lambda: self.controls.mindset,
        )
        self.battle = BattleRunner(self.ctx, self.model, self.controls._driver_scale)

        def _on_rewind(t: float) -> None:
            dispatcher.purge(reason="flashback", now=t)
            self.model.rewind(t)
            self.planner.rewind(t)

        state.rewind_listeners.append(_on_rewind)
        state.session_listeners.append(self._on_new_session)
        state.session_end_listeners.append(self.fold_session_end)

    def setup_voice(self) -> str | None:
        return self.voice_controller.setup_voice()

    def enable_voice(self, worker: VoiceWorker) -> None:
        return self.voice_controller.enable_voice(worker)

    def close_voice(self) -> None:
        return self.voice_controller.close_voice()

    def voice_payload(self, now: float) -> dict[str, Any]:
        return self.voice_controller.voice_payload(now)

    def menu_payload(self, now: float) -> dict[str, object]:
        return self.menu_controller.menu_payload(now)

    def driver_balance(self, lap: int) -> str:
        return self.menu_controller.driver_balance(lap)

    @property
    def mindset(self) -> str:
        return self.controls.mindset

    def mode(self) -> dict[str, Any]:
        return self.controls.mode()

    def set_mindset(self, name: str, now: float) -> bool:
        return self.controls.set_mindset(name, now)

    def cycle_budget(self) -> float:
        return self.controls.cycle_budget()

    def cycle_mindset(self, now: float) -> None:
        return self.controls.cycle_mindset(now)

    def set_page(self, name: str, now: float, *, manual: bool = True) -> bool:
        return self.controls.set_page(name, now, manual=manual)

    def reset_page(self, now: float) -> None:
        return self.controls.reset_page(now)

    def client_message(self, msg: dict[str, Any]) -> None:
        return self.controls.client_message(msg)

    def client_press(self, down: bool) -> None:
        return self.controls.client_press(down)

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

    def _on_new_session(self, uid: int) -> None:
        self.writer.flush_previous_session()
        self.ctx.dispatcher.reset_session(uid)
        self.planner._held_plan = None
        self._rule_errors_logged.clear()
        self.model._reset_energy_lap_tracking()
        self.menu_controller.reset()
        self.controls.reset()
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
        press = self.controls.detector.tick(now)
        if press is not None:
            self.controls._press_queue.append(press)
        balance = self.menu_controller.driver_balance(snapshot.lap_num)
        if balance != snapshot.driver_balance:
            snapshot = dataclasses.replace(snapshot, driver_balance=balance)
        self.ctx.last_snapshot = snapshot
        for p in self.controls._press_queue:
            if self.menu_controller._menu_press(p, snapshot):
                continue
            if p.kind == "mindset":
                self.controls.cycle_mindset(p.t)
            elif p.kind == "page":
                self.controls.reset_page(p.t)
            else:
                self.ctx.dispatcher.on_press(p, snapshot)
        self.controls._press_queue.clear()
        if self.ctx.menu.expired(self.ctx.store.current().menu, now):
            self.menu_controller._menu_close(now, snapshot, "timeout")
        self.voice_controller._voice_tick(now, snapshot)
        self.controls._auto_page(snapshot, now)
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
