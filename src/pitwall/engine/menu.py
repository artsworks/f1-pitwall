from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from typing import Any

from pitwall.config.models import (
    MenuItemModel,
)
from pitwall.engine.context import EngineContext
from pitwall.engine.controls import Controls
from pitwall.engine.voice import VoiceController
from pitwall.input.menu import ReplyPicker, answer
from pitwall.input.press import Press
from pitwall.rules.engine import STALENESS_DEFAULT_S
from pitwall.rules.expr import expr_names, namespace_data
from pitwall.state.session import Snapshot


class MenuController:
    def __init__(
        self,
        ctx: EngineContext,
        controls: Controls,
        voice: VoiceController,
        budget_label: Callable[[float], str],
    ) -> None:
        self.ctx = ctx
        self.controls = controls
        self.voice = voice
        self._budget_label = budget_label
        self._menu_replies = ReplyPicker()
        self._menu_signal_names: dict[str, frozenset[str]] = {}
        self._menu_signal_hash = self.ctx.store.hash

    def reset(self) -> None:
        self.ctx.menu.close()
        self.voice.reset()
        self._menu_replies.reset()
        self.ctx.opinions.clear()

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
                self.voice._voice_open(p.t, "menu")
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
        self.voice._voice_close(t, "menu")
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
        self.voice._voice_close(t, "menu")
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
        case, values = answer(item, snap, self.controls.mindset)
        if item.action == "budget":
            budget = self.controls.cycle_budget()
            values["budget"] = self._budget_label(budget)
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
            self.controls.cycle_mindset(t)
        elif item.action == "silent":
            self.ctx.dispatcher.toggle_silent(snap)
        elif item.action == "page":
            self.controls.reset_page(t)
        if text:
            self.ctx.dispatcher.menu_reply(text, f"menu:{item.id}", snap, item.related_rules)

    def driver_balance(self, lap: int) -> str:
        """Latest balance opinion while it still holds (docs/12 advice bias)."""
        held = self.ctx.opinions.get("balance")
        hold = self.ctx.store.current().menu.opinion_hold_laps
        if held is None or lap - held[1] > hold:
            return ""
        return held[0]
