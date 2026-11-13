from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pitwall.config.models import (
    Settings,
    call_budget,
    resolve_mindset,
)
from pitwall.engine.context import EngineContext
from pitwall.engine.voice import VoiceController
from pitwall.input.press import Press, PressDetector
from pitwall.model.driver import (
    DriverScale,
    advice_scale,
)
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import Snapshot

_MENU_CLIENT_OPS = {
    "up": "menu_up",
    "down": "menu_down",
    "confirm": "menu_confirm",
    "close": "menu_close",
}


class Controls:
    def __init__(
        self, ctx: EngineContext, rule_engine: RuleEngine | None, voice: VoiceController
    ) -> None:
        self.ctx = ctx
        self.rule_engine = rule_engine
        self.voice = voice
        inp = ctx.store.current().input
        self.detector = PressDetector(
            double_ms=inp.double_press_ms,
            long_ms=inp.long_press_ms,
            bounce_ms=inp.bounce_ms,
        )
        self._press_queue: list[Press] = []
        ctx.state.press_listeners.append(self._on_press_edge)
        ctx.state.toggle_listeners.append(lambda t: self._press_queue.append(Press("silent", t)))
        ctx.state.action_listeners.append(lambda k, t: self._press_queue.append(Press(k, t)))
        pages = ctx.store.current().ui.pages
        ctx.page = pages[0] if pages else "race"

    def reset(self) -> None:
        pass

    def _on_press_edge(self, t: float, down: bool) -> None:
        press = self.detector.edge(t, down)
        if press is not None:
            self._press_queue.append(press)

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
                self.voice._voice_close(now, "tap")
            else:
                self.voice._voice_open(now, "key", self.ctx.store.current().voice.key_max_open_s)
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

    def _plan_thresholds(self, snap: Snapshot) -> Mapping[str, object]:
        th: dict[str, object] = {**self.ctx.store.current().thresholds, **self.mode()}
        if not (snap.is_sprint or snap.short_race):
            return th
        # no mandatory stop in a sprint or a 5-lap race
        return {**th, "plan_two_compound_rule": 0}
