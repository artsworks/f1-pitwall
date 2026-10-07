"""Find recurring menu questions that arrive before their related calls."""

from __future__ import annotations

import ast
import json
import math
from collections.abc import Iterable, Mapping
from statistics import median
from typing import Any

from pitwall.config.loader import ConfigStore
from pitwall.config.models import MenuSettings, RuleDefModel
from pitwall.store.db import Database


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("inputs")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _threshold_names(source: str) -> set[str]:
    tree = ast.parse(source, mode="eval")
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "th"
    }


def question_candidates(
    db: Database,
    menu: MenuSettings,
    rules: Iterable[RuleDefModel],
    window_laps: int = 2,
    min_asks: int = 2,
    *,
    thresholds: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return recurring menu questions that were not covered by a call."""
    window = max(0, int(window_laps))
    minimum = max(1, int(min_asks))
    rule_by_id = {rule.id: rule for rule in rules}
    items = {item.id: item for item in menu.items if item.related_rules}
    threshold_values = dict(
        ConfigStore().current().thresholds if thresholds is None else thresholds
    )
    grouped: dict[str, dict[str, Any]] = {}

    for session in db.sessions():
        uid = session.get("uid")
        if uid is None:
            continue
        fired: dict[str, list[tuple[float, float]]] = {}
        for call in db.calls_for_session(int(uid)):
            if call.get("outcome") != "fired":
                continue
            lap = _number(call.get("lap"))
            t = _number(call.get("t"))
            rule_id = call.get("rule_id")
            if lap is None or t is None or not isinstance(rule_id, str):
                continue
            fired.setdefault(rule_id, []).append((lap, t))

        session_items: dict[str, list[tuple[bool, dict[str, Any]]]] = {}
        for row in db.driver_inputs_for_session(int(uid)):
            if row.get("kind") != "question":
                continue
            item_id = row.get("item_id")
            item = items.get(str(item_id))
            if item is None:
                continue
            lap = _number(row.get("lap"))
            t = _number(row.get("t"))
            if lap is None or t is None:
                continue
            covered = any(
                call_lap >= lap - window and call_lap <= lap and call_t <= t
                for rule_id in item.related_rules
                for call_lap, call_t in fired.get(rule_id, [])
            )
            values = _inputs(row).get("signals")
            signals = values if isinstance(values, dict) else {}
            session_items.setdefault(item.id, []).append((not covered, signals))

        for item_id, asks in session_items.items():
            uncovered = [signals for is_uncovered, signals in asks if is_uncovered]
            if len(uncovered) < minimum:
                continue
            entry = grouped.setdefault(
                item_id,
                {
                    "item": items[item_id],
                    "sessions": 0,
                    "asks": 0,
                    "uncovered": 0,
                    "signals": [],
                },
            )
            entry["sessions"] += 1
            entry["asks"] += len(asks)
            entry["uncovered"] += len(uncovered)
            entry["signals"].extend(uncovered)

    candidates: list[dict[str, Any]] = []
    for item_id, entry in grouped.items():
        item = entry["item"]
        signal_values: dict[str, list[float]] = {}
        for signals in entry["signals"]:
            for name, value in signals.items():
                number = _number(value)
                if number is not None:
                    signal_values.setdefault(str(name), []).append(number)
        at_ask = {
            name: round(float(median(values)), 3)
            for name, values in sorted(signal_values.items())
            if values
        }
        related_rules: list[dict[str, Any]] = []
        for rule_id in item.related_rules:
            rule = rule_by_id.get(rule_id)
            if rule is None:
                continue
            names = _threshold_names(rule.when)
            related_rules.append(
                {
                    "rule_id": rule.id,
                    "thresholds": {
                        name: threshold_values[name]
                        for name in sorted(names)
                        if name in threshold_values
                    },
                }
            )
        candidates.append(
            {
                "item_id": item_id,
                "label": item.label,
                "sessions": entry["sessions"],
                "asks": entry["asks"],
                "uncovered": entry["uncovered"],
                "window_laps": window,
                "at_ask": at_ask,
                "rules": related_rules,
                "suggestion": "fire earlier: review these thresholds",
            }
        )
    candidates.sort(key=lambda candidate: (-candidate["uncovered"], candidate["item_id"]))
    return candidates
