"""Engine: snapshot -> evaluate -> submit, driven live or by replay.

Replay ticks whenever the record clock crosses a tick boundary, so a 10x or
max-speed replay makes identical decisions to 1x (determinism per docs/07).
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import math
import time
import traceback
from pathlib import Path
from statistics import median
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, CallSink, Dispatcher, LogSink
from pitwall.clock import Clock, ReplayClock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import InputSettings, MenuItemModel, resolve_mindset
from pitwall.ingest import Ingest
from pitwall.input.menu import DriverMenu, ReplyPicker, answer
from pitwall.input.press import Press, PressDetector
from pitwall.metrics import Metrics
from pitwall.model.budget import EnergyBudget, FuelBudget, energy_budget, fuel_budget
from pitwall.model.deg import DegFit, Prior, fit_stint, laps_of_pace, resolve_prior
from pitwall.model.pitloss import current_pit_loss, measure, ref_pace_ms
from pitwall.net.recording import RecordingReader
from pitwall.rules.engine import RuleEngine
from pitwall.state.lap import LapSummary
from pitwall.state.model_view import ModelView
from pitwall.state.session import SessionState, Snapshot
from pitwall.store.db import LapRow
from pitwall.strategy.battle import (
    HOLD,
    PASS_COMPOUND,
    PASS_DRS,
    PASS_NODRS,
    Battle,
    BattleInputs,
    BattleRates,
    BattleTracker,
    Episode,
    shrink,
)
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


def action_bits(inp: InputSettings) -> dict[str, int]:
    """Named UDP Action buttons beyond ack/silent (docs/12)."""
    return {
        "mindset": int(inp.mindset_toggle_bit),
        "page": int(inp.page_cycle_bit),
        "menu_up": int(inp.menu_up_bit),
        "menu_down": int(inp.menu_down_bit),
        "menu_close": int(inp.menu_close_bit),
    }


_MENU_CLIENT_OPS = {
    "up": "menu_up",
    "down": "menu_down",
    "confirm": "menu_confirm",
    "close": "menu_close",
}


def _with_plan[T: (ModelView, Snapshot)](obj: T, f: PlanFields) -> T:
    return dataclasses.replace(
        obj,
        plans=f.plans,
        active_plan=f.active_plan,
        on_plan=f.on_plan,
        plan_label=f.plan_label,
        plan_spoken=f.plan_spoken,
        plan_stops_left=f.plan_stops_left,
        plan_target_lap=f.plan_target_lap,
        plan_window_start=f.plan_window_start,
        plan_window_end=f.plan_window_end,
        plan_window_text=f.plan_window_text,
        plan_window_open=f.plan_window_open,
        plan_next_compound=f.plan_next_compound,
        plan_off_s=f.plan_off_s,
        plan_switch_count=f.plan_switch_count,
        plan_switched_from=f.plan_switched_from,
        plan_switch_reason=f.plan_switch_reason,
        plan_switch_lap=f.plan_switch_lap,
        plan_target_shift=f.plan_target_shift,
        plan_b_spoken=f.plan_b_spoken,
        plan_b_delta_s=f.plan_b_delta_s,
        plan_c_spoken=f.plan_c_spoken,
        plan_c_delta_s=f.plan_c_delta_s,
    )


_NON_GREEN_REASONS = frozenset({"first_lap", "pitted", "after_in_lap", "safety_car", "flashback"})


class Engine:
    def __init__(
        self,
        store: ConfigStore,
        clock: Clock,
        ingest: Ingest,
        state: SessionState,
        rule_engine: RuleEngine | None,
        dispatcher: Dispatcher,
    ) -> None:
        self.store = store
        self.clock = clock
        self.ingest = ingest
        self.state = state
        self.rule_engine = rule_engine
        self.dispatcher = dispatcher
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
        self.opinions: dict[str, tuple[str, int]] = {}  # topic -> (item id, lap)
        self._apply_mode()
        # Crash recovery (docs/18): heartbeat to SQLite; on restart replay the
        # recording tail with the rules muted.
        self.recording_path_source: Any = None  # callable -> Path | None
        self._last_heartbeat: float | None = None
        self.recovering = False
        self.tick_errors = 0
        self.db: Any = dispatcher.log.db
        self.dispatcher.tuned_cooldown = load_cooldown_mults(self.db)
        self._laps_written = 0
        self._session_upserted: int | None = None
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
        self._plan_key: tuple[int, int, bool, bool, int] | None = None
        self._stops_done = 0
        self._used_compounds: set[int] = set()
        self._prev_race_phase = ""
        self._compound_priors: dict[int, DegFit] = {}
        # Battle state + learned pass model (docs/20 L3).
        self.battle_tracker = BattleTracker()
        self._battle_rates: tuple[int, BattleRates] | None = None
        self.fuel_budget: FuelBudget | None = None
        self.energy_budget: EnergyBudget | None = None
        self._pit_in_lap: LapSummary | None = None
        self._folded_stints: set[tuple[int, int]] = set()
        self._fuel_last_kg: float | None = None
        self._session_fuel_last_kg: float | None = None
        self._session_fuel_deltas: list[float] = []
        self._stint_fuel_ref = 0.0
        self._stint_laps: list[LapRow] = []
        self._model_key: tuple[int | None, int, int] | None = None
        self._fuel_prior = Prior(0.0, 0.0, "default")
        self._wear_per_lap = 0.0

        def _on_rewind(t: float) -> None:
            dispatcher.purge(reason="flashback", now=t)

        state.rewind_listeners.append(_on_rewind)
        state.session_listeners.append(self._on_new_session)

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
            target = "battle"
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
        if p.kind in ("menu_up", "menu_down"):
            was_open = self.menu.open
            item = self.menu.step(settings, -1 if p.kind == "menu_up" else 1, p.t)
            if item is None:
                return True
            if not was_open:
                self._menu_log(p.t, snapshot, "menu_open", None, "")
            if settings.speak_on_scroll:
                self.dispatcher.menu_prompt(item.label, snapshot)
            return True
        if p.kind == "menu_close":
            self._menu_close(p.t, snapshot, "close")
            return True
        if not self.menu.open:
            return p.kind == "menu_confirm"
        if p.kind in ("ack", "menu_confirm"):
            self._menu_confirm(p.t, snapshot)
            return True
        if p.kind in ("neg", "bookmark"):
            self._menu_close(p.t, snapshot, "cancel")
            return True
        return False

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
        inputs: dict[str, str] | None = None,
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
        if item is None:
            return
        snap = dataclasses.replace(snapshot, now=t)
        case, values = answer(item, snap, self.mindset)
        if item.action == "budget":
            values["budget"] = str(self.cycle_budget())
        text = self._menu_replies.pick(item, case, values)
        self._menu_log(t, snap, "driver_input", item, text, {"case": case, **values})
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
        self.dispatcher.reset_session()
        self.menu.close()
        self._menu_replies.reset()
        self.opinions.clear()
        self._laps_written = len(self.state.laps)
        self._session_ended_written = False
        self._pit_in_lap = None
        self._folded_stints.clear()
        self._fuel_last_kg = None
        self._session_fuel_last_kg = None
        self._session_fuel_deltas = []
        self.deg_fit = None
        self.plan_tracker.reset()
        self._plan_key = None
        self.battle_tracker.reset()
        self._battle_rates = None
        self._stops_done = 0
        self._used_compounds.clear()
        self._prev_race_phase = ""
        self._upsert_session(uid)

    def _upsert_session(self, uid: int) -> None:
        if self.db is None:
            return
        self._session_upserted = uid
        snap = self.state.snapshot(self.clock.now())
        self.db.upsert_session(
            uid,
            track_id=getattr(snap, "track_id", 0),
            session_type=getattr(snap, "session_type", 0),
            started_at=self.clock.now(),
            game_version=getattr(snap, "game_version", ""),
            config_hash=self.store.hash,
            weather=getattr(snap, "weather", 0),
            game_mode=getattr(snap, "game_mode", 0),
        )

    def _write_laps(self) -> None:
        if self.db is None or self.state.session_uid is None:
            return
        if self.recovering:
            # Rebuilt laps are already committed; don't insert or fold twice.
            self.state.rival_laps.clear()
            self._laps_written = len(self.state.laps)
            return
        uid = self.state.session_uid
        for car_idx, lap in self.state.rival_laps:
            self.db.insert_lap(uid, car_idx, lap)
        self.state.rival_laps.clear()
        new_laps = self.state.laps[self._laps_written :]
        for lap in new_laps:
            self.db.insert_lap(uid, 0, lap)
        self._laps_written = len(self.state.laps)
        for lap in new_laps:
            self._on_player_lap(uid, lap)

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
            boundary = tail[0].lap_num
            key = (uid, boundary)
            if key not in self._folded_stints:
                self._folded_stints.add(key)
                prev = self._stint_rows(all_laps[: len(all_laps) - len(tail)])
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
                    if fit.source == "fit":
                        for name, value in (
                            ("deg_ms_per_lap", fit.deg_ms_per_lap),
                            ("base_ms", fit.base_ms),
                            ("fuel_ms_per_lap", fit.fuel_ms_per_lap),
                        ):
                            db.fold_param(
                                track_id,
                                prev[0].compound,
                                name,
                                value,
                                weight=float(fit.n),
                                param_weight_cap=self._th("param_weight_cap", 50),
                            )

        if tail:
            self._stint_laps = tail
            self._stint_fuel_ref = max((r.fuel_remaining_laps for r in tail), default=0.0)
            compound = tail[0].compound
            prior = self._deg_prior(track_id, compound, settings)
            fuel_param = db.get_param(track_id, compound, "fuel_ms_per_lap")
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
            db.upsert_stint(uid, 0, compound, tail[0].lap_num, tail[-1].lap_num, fit)

        # Pit loss: measure when an out-lap completes against a pending in-lap.
        if "pitted" in lap.invalid_reasons:
            self._pit_in_lap = lap
        elif "after_in_lap" in lap.invalid_reasons and self._pit_in_lap is not None:
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
                    pit = measure(
                        in_row,
                        out_row,
                        self.state.pit_lane_time_ms,
                        ref,
                        neutralised,
                    )
                    db.insert_pit_event(
                        uid,
                        0,
                        in_lap.lap_num,
                        pit.loss_ms,
                        neutralised,
                        pit.lane_ms,
                        pit.in_lap_ms,
                        pit.out_lap_ms,
                        pit.ref_pace_ms,
                    )
                    suffix = {0: "green", 1: "sc", 2: "vsc"}.get(neutralised, "green")
                    db.fold_param(
                        track_id,
                        0,
                        f"pit_loss_{suffix}_ms",
                        float(pit.loss_ms),
                        param_weight_cap=self._th("param_weight_cap", 50),
                    )

        # Fuel burn per lap: fold each consecutive-valid-lap delta.
        if lap.valid and lap.fuel_kg > 0:
            if self._fuel_last_kg is not None:
                delta = self._fuel_last_kg - lap.fuel_kg
                if 0.0 < delta < 10.0:
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
        deg_p = resolve_prior(
            self.db,
            track_id,
            compound,
            "deg_ms_per_lap",
            overlay_value=(overlay.deg_ms_per_lap.get(compound) if overlay is not None else None),
            default=self._th("deg_default_ms_per_lap", 80),
            min_weight=min_w,
        )
        base_p = resolve_prior(
            self.db,
            track_id,
            compound,
            "base_ms",
            overlay_value=(
                float(overlay.base_pace_ms)
                if overlay is not None and overlay.base_pace_ms > 0
                else None
            ),
            default=self._th("release_fallback_lap_s", 95.0) * 1000.0,
            min_weight=min_w,
        )
        fuel_p = resolve_prior(
            self.db,
            track_id,
            compound,
            "fuel_ms_per_lap",
            overlay_value=None,
            default=self._th("fuel_ms_per_lap_default", 30),
            min_weight=min_w,
        )
        return DegFit(
            base_ms=base_p.value,
            deg_ms_per_lap=deg_p.value,
            fuel_ms_per_lap=fuel_p.value,
            n=0,
            rmse_ms=0.0,
            confidence={"learned": 0.5, "overlay": 0.35}.get(deg_p.source, 0.2),
            source=deg_p.source,
        )

    def _th(self, name: str, default: float) -> float:
        v = self.store.current().thresholds.get(name, default)
        return float(v) if isinstance(v, int | float) else default

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
        per_lap_kg, fuel_source = self._fuel_per_lap(laps_remaining)
        self.fuel_budget = fuel_budget(
            laps_remaining=laps_remaining,
            fuel_in_tank_kg=state.fuel_in_tank,
            per_lap_kg=per_lap_kg,
            source=fuel_source,
        )
        self.energy_budget = energy_budget(
            store_j=state.ers_store_energy_j,
            store_capacity_j=self._th("ers_store_capacity_j", 4_000_000),
            laps_remaining=laps_remaining,
            deployed_this_lap_j=state.ers_deployed_this_lap_j,
            harvested_this_lap_j=state.ers_harvested_mguk_j + state.ers_harvested_mguh_j,
            harvest_limit_per_lap_j=state.ers_harvest_limit_per_lap_j,
            soc_floor_pct=float(mode.get("ers_soc_floor_pct", 0) or 0),
            over_tolerance_j=self._th("energy_over_tolerance_j", 200_000),
            attack_ok=mode.get("ers_policy") == "attack_rival",
        )

        fit = self.deg_fit
        wear_mean = sum(state.tyres_wear.as_tuple()) / 4.0
        lop = math.inf
        predicted = 0
        if fit is not None:
            lop = laps_of_pace(
                fit,
                state.tyre_age_laps,
                wear_mean,
                cliff_ms=self._th("tyre_cliff_ms", 1500),
                wear_cliff_pct=self._th("wear_cliff_pct", 70),
                wear_per_lap=self._wear_per_lap,
            )
            burned = self._stint_fuel_ref - state.fuel_remaining_laps
            predicted = int(
                fit.base_ms
                + fit.deg_ms_per_lap * (state.tyre_age_laps + 1)
                + fit.fuel_ms_per_lap * (burned + 1)
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
        behind = (
            RivalView(
                snap.rival_behind_idx,
                snap.rival_behind_name,
                snap.rival_behind_pace_ms,
                snap.rival_behind_pitted,
            )
            if snap.rival_behind_idx >= 0
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
            rival_behind=behind,
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
            key = (snap.lap_num, self._stops_done, neutral, cheap, snap.tyre_visual)
            if key != self._plan_key:
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
                    self.store.current().thresholds,
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
            PASS_DRS: self._th("battle_pass_drs_prior", 0.35),
            PASS_NODRS: self._th("battle_pass_nodrs_prior", 0.15),
            HOLD: self._th("battle_hold_prior", 0.7),
        }
        vals: dict[str, float] = {}
        for name, prior in priors.items():
            p = self.db.get_param(track_id, PASS_COMPOUND, name) if self.db else None
            vals[name] = shrink(p.value if p else None, p.weight if p else 0.0, prior, w)
        rates = BattleRates(vals[PASS_DRS], vals[PASS_NODRS], vals[HOLD])
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
            drs_available=snap.drs_available,
            attack_gap_s=float(attack) if isinstance(attack, int | float) else 1.0,
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
                "drs": ep.drs,
                "result": ep.result,
            }
        )
        if self.db is None or snap.track_id < 0:
            return
        name = HOLD if ep.kind == "defend" else PASS_DRS if ep.drs else PASS_NODRS
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

    def tick(self, now: float) -> list[Call]:
        self.store.poll(now)
        if self.state.track_id != self._track_loaded:
            self._track_loaded = self.state.track_id
            self.store.set_track(self.state.track_id if self.state.track_id >= 0 else None)
        self._write_laps()
        self._update_model()
        snapshot = self._battle(self._plan(self.state.snapshot(now)))
        if (
            snapshot.session_ended
            and not self._session_ended_written
            and self.db is not None
            and self.state.session_uid is not None
        ):
            self._session_ended_written = True
            self.db.end_session(self.state.session_uid, now)
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
        if (
            self.db is not None
            and self.state.session_uid is not None
            and self._session_upserted != self.state.session_uid
        ):
            self._upsert_session(self.state.session_uid)
        self._heartbeat(now)
        if self.recovering:
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

    async def run_live(self) -> None:
        """Tick at tick_hz forever; dispatch drains on its own loop. The
        watchdog keeps the loop alive through a failing tick (logged)."""
        period = self.tick_period
        while True:
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
    decision_log_path: Path | None = None,
    decision_log_fp: Any = None,
    sinks: list[CallSink] | None = None,
    record_to: Path | None = None,
    db: Any = None,
) -> Engine:
    """Assemble a full engine from the layered config. db=None disables
    SQLite mirroring (replays opt in via the CLI)."""
    store = ConfigStore(overrides=overrides, rules_dir=rules_dir)
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
        sinks=sinks or [LogSink()],
        metrics=Metrics(),
        budget_override=mode.get("call_budget_per_lap"),
        input=settings.input,
    )
    return Engine(store, clock, ingest, state, rule_engine, dispatcher)


def build_census_engine(clock: Clock | None = None) -> Engine:
    """Ingest-only engine for --no-rules census replays."""
    store = ConfigStore()
    clock = clock or VirtualClock()
    ingest = Ingest()
    state = SessionState()
    dlog = DecisionLog()
    dispatcher = Dispatcher(store.current().policy, clock, decision_log=dlog, sinks=[])
    return Engine(store, clock, ingest, state, None, dispatcher)


def record_and_run(path: Path, speed: float | None) -> tuple[int, list[Call]]:
    engine = build_engine(clock=VirtualClock() if speed is None else ReplayClock(speed))
    return asyncio.run(run_replay(path, engine, speed))
