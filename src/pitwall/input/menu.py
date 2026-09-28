"""Driver -> pit wall menu (docs/12): backend-owned state, deterministic answers.

Up/Down open and scroll the menu, UDP Action 1 confirms. Answers are picked
from YAML reply templates keyed by a case the snapshot selects (ADR 0008:
no LLM); variants rotate per (item, case)."""

from __future__ import annotations

import math
import string
from collections.abc import Callable, Mapping

from pitwall.config.models import InputSettings, MenuItemModel, MenuSettings
from pitwall.rules.expr import ExprError, Predicate
from pitwall.state.session import Snapshot

Answer = tuple[str, dict[str, str]]  # (case, template values)


_PREDICATES: dict[str, Predicate] = {}


def _holds(source: str, ns: Mapping[str, object]) -> bool:
    pred = _PREDICATES.get(source)
    if pred is None:
        pred = _PREDICATES[source] = Predicate(source)
    try:
        return bool(pred(ns))
    except Exception:
        return False


def situational(items: list[MenuItemModel], ns: Mapping[str, object] | None) -> list[MenuItemModel]:
    """Items relevant to the situation in `ns` (a rule namespace), most
    relevant first. Everything, in YAML order, when `ns` is None."""
    if ns is None:
        return list(items)
    shown = [i for i in items if not i.show_when or _holds(i.show_when, ns)]
    top = [i for i in shown if i.rank_when and _holds(i.rank_when, ns)]
    return top + [i for i in shown if i not in top] or list(items)


class DriverMenu:
    def __init__(self) -> None:
        self.open = False
        self.index = 0
        self.last_t = 0.0
        self.items: list[MenuItemModel] = []

    def step(
        self,
        settings: MenuSettings,
        delta: int,
        t: float,
        ns: Mapping[str, object] | None = None,
    ) -> MenuItemModel | None:
        """Open (Down -> first item, Up -> last) or move the highlight. The
        item list is picked from `ns` on open and frozen while open."""
        if not settings.enabled or not settings.items:
            return None
        if not self.open:
            self.items = situational(settings.items, ns)
        items = self.items
        n = len(items)
        if not self.open:
            self.open = True
            self.index = 0 if delta > 0 else n - 1
        elif settings.wrap:
            self.index = (self.index + delta) % n
        else:
            self.index = max(0, min(n - 1, self.index + delta))
        self.last_t = t
        return items[self.index]

    def selected(self, settings: MenuSettings) -> MenuItemModel | None:
        if not self.open or not self.items:
            return None
        return self.items[min(self.index, len(self.items) - 1)]

    def close(self) -> None:
        self.open = False

    def expired(self, settings: MenuSettings, now: float) -> bool:
        return self.open and settings.timeout_s > 0 and now - self.last_t >= settings.timeout_s

    def payload(self, settings: MenuSettings, now: float) -> dict[str, object]:
        if not self.open or not self.items:
            return {"open": False}
        left = settings.timeout_s - (now - self.last_t) if settings.timeout_s > 0 else None
        return {
            "open": True,
            "index": min(self.index, len(self.items) - 1),
            "items": [i.label for i in self.items],
            "left_s": max(0.0, round(left, 1)) if left is not None else None,
            "timeout_s": settings.timeout_s,
        }


# -- answers -------------------------------------------------------------------


def _n(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}" if math.isfinite(x) else "?"


def _laps(x: float) -> str:
    if not math.isfinite(x):
        return "plenty of"
    return str(max(0, round(x)))


def _common(snap: Snapshot, mindset: str) -> dict[str, str]:
    return {
        "lap": str(snap.lap_num),
        "laps_left": str(snap.laps_remaining),
        "mindset": mindset,
    }


def _tyres(snap: Snapshot) -> Answer:
    v = {
        "wear": _n(snap.wear_max_pct, 0),
        "age": str(snap.tyre_age_laps),
        "pace_laps": _laps(snap.laps_of_pace),
        "to": snap.tyre_switch_to,
    }
    if snap.wear_max_pct <= 0 and snap.tyre_age_laps == 0:
        return "unknown", v
    if snap.tyre_switch_to:
        return "switch", v
    if snap.laps_of_pace < 1 or snap.wear_max_pct >= 70:
        return "gone", v
    if snap.laps_of_pace < 4 or snap.wear_max_pct >= 55:
        return "fading", v
    return "ok", v


def _pit(snap: Snapshot) -> Answer:
    plan = snap.pit_plan
    v = {
        "plan_lap": str(snap.pit_plan_lap),
        "in_laps": str(max(0, snap.pit_plan_lap - snap.lap_num)),
        "reason": snap.pit_plan_reason,
        "gain": _n(snap.pit_plan_gain_s),
        "window": f"{snap.pit_window_start} to {snap.pit_window_end}"
        if snap.pit_window_start
        else "",
    }
    if plan in ("box_now", "cheap_stop", "free_stop"):
        return "box_now", v
    if plan == "box_in_n":
        return "soon", v
    if plan == "no_stop":
        return "no_stop", v
    if plan in ("stay", "overcut"):
        return "stay_out", v
    return "unknown", v


def _gap(snap: Snapshot) -> Answer:
    v = {
        "gap": _n(snap.gap_ahead_s),
        "name": snap.rival_ahead_name or "the car ahead",
        "trend": _n(abs(snap.gap_trend_ahead_s)),
    }
    if snap.rival_ahead_idx < 0 or not math.isfinite(snap.gap_ahead_s):
        return "none", v
    if snap.gap_trend_ahead_s > 0.1:
        return "closing", v
    if snap.gap_trend_ahead_s < -0.1:
        return "opening", v
    return "steady", v


def _gap_behind(snap: Snapshot) -> Answer:
    v = {
        "gap": _n(snap.gap_behind_s),
        "name": snap.rival_behind_name or "the car behind",
        "trend": _n(abs(snap.gap_trend_behind_s)),
    }
    if snap.rival_behind_idx < 0 or not math.isfinite(snap.gap_behind_s):
        return "none", v
    if snap.gap_trend_behind_s > 0.1:
        return "closing", v
    return "steady", v


def _fuel(snap: Snapshot) -> Answer:
    m = snap.fuel_margin_laps
    v = {"margin": _n(abs(m))}
    if not snap.fuel_source and snap.fuel_remaining_laps == 0:
        return "unknown", v
    if m < -0.2:
        return "short", v
    if m < 0.3:
        return "tight", v
    if m > 1.0:
        return "spare", v
    return "ok", v


def _plan(snap: Snapshot) -> Answer:
    case, v = _pit(snap)
    v["compound_sets"] = str(snap.fresh_sets_medium + snap.fresh_sets_hard)
    if case == "box_now":
        return "box_now", v
    if case in ("soon", "stay_out") and snap.pit_plan_lap > snap.lap_num:
        return "stop", v
    if case == "no_stop":
        return "to_end", v
    return "unknown", v


_CROSSOVER_TYRE = {"to_inter": 7, "to_wet": 8}


def _rain(snap: Snapshot) -> Answer:
    """Forecast rain chance plus what it means for the tyre we're on: the
    field's lap times decide a switch, a forecast crossover only warns."""
    peak = max(snap.rain_pct_in_10, snap.rain_pct_in_30)
    to = {"to_inter": "inters", "to_wet": "wets", "to_dry": "slicks"}.get(
        snap.weather_crossover, ""
    )
    v = {
        "in10": str(snap.rain_pct_in_10),
        "in30": str(snap.rain_pct_in_30),
        "to": snap.tyre_switch_to or to,
        "tyre": {7: "inters", 8: "wets"}.get(snap.tyre_compound, "slicks"),
    }
    if snap.tyre_switch_to:
        return "switch", v
    on_it = _CROSSOVER_TYRE.get(snap.weather_crossover, 0) == snap.tyre_compound or (
        snap.weather_crossover == "to_dry" and snap.tyre_compound not in (7, 8)
    )
    if snap.weather_crossover and on_it:
        return "right_tyre", v
    if snap.weather_crossover:
        return "crossover", v
    if peak >= 50:
        return "coming", v
    if peak >= 20:
        return "chance", v
    return "dry", v


def _push(snap: Snapshot) -> Answer:
    v = {"margin": _n(abs(snap.fuel_margin_laps)), "gap": _n(snap.gap_ahead_s)}
    if snap.fuel_source and snap.fuel_margin_laps < -0.2:
        return "save_fuel", v
    if snap.energy_mode == "over":
        return "save_energy", v
    if snap.laps_remaining > 0 and snap.laps_of_pace < min(snap.laps_remaining, 4):
        return "save_tyres", v
    if 0 < snap.gap_ahead_s <= 1.0:
        return "attack", v
    return "push", v


def _lap_time(ms: float) -> str:
    if ms <= 0 or not math.isfinite(ms):
        return "?"
    m, sec = divmod(ms / 1000.0, 60.0)
    return f"{int(m)}:{sec:04.1f}"


def _race_stat(snap: Snapshot) -> Answer:
    """The one fact that matters most right now: a critical fuel, tyre or
    energy problem, then the pit call, else position and laps left."""
    pit_case, v = _pit(snap)
    v.update(
        {
            "pos": str(snap.position),
            "best": _lap_time(snap.player_best_lap_ms),
            "margin": _n(abs(snap.fuel_margin_laps)),
            "wear": _n(snap.wear_max_pct, 0),
        }
    )
    if snap.fuel_source and snap.fuel_margin_laps < -0.2:
        return "fuel_short", v
    if snap.session_kind == "practice":
        if snap.wear_max_pct >= 70:
            return "tyres_gone", v
        return ("practice" if snap.player_best_lap_ms > 0 else "practice_no_best"), v
    if snap.wear_max_pct >= 70 or (snap.tyre_age_laps > 0 and snap.laps_of_pace < 1):
        return "tyres_gone", v
    if snap.energy_mode == "over":
        return "energy", v
    if pit_case == "box_now":
        return "box_now", v
    if pit_case == "soon":
        return "pit_soon", v
    if snap.position <= 0:
        return "unknown", v
    return "position", v


def _side(name: str, gap: float, trend: float, ahead: bool) -> str:
    """Gap and which way it's going, per lap: "GASLY 0.4 behind, catching 0.3 a lap"."""
    where = "ahead" if ahead else "behind"
    out = f"{name} {_n(gap)} {where}"
    if trend > 0.05:
        out += f", we're catching {_n(trend)} a lap" if ahead else f", catching {_n(trend)} a lap"
    elif trend < -0.05:
        out += f", pulling {_n(-trend)} a lap" if ahead else f", we're pulling {_n(-trend)} a lap"
    else:
        out += ", steady"
    return out + "."


def _fight(snap: Snapshot) -> Answer:
    has_ahead = snap.rival_ahead_idx >= 0 and math.isfinite(snap.gap_ahead_s)
    has_behind = snap.rival_behind_idx >= 0 and math.isfinite(snap.gap_behind_s)
    v = {"ahead": "", "behind": ""}
    if has_ahead:
        v["ahead"] = _side(
            snap.rival_ahead_name or "Car", snap.gap_ahead_s, snap.gap_trend_ahead_s, True
        )
    if has_behind:
        v["behind"] = _side(
            snap.rival_behind_name or "Car", snap.gap_behind_s, snap.gap_trend_behind_s, False
        )
    if has_ahead and has_behind:
        return "both", v
    if has_ahead:
        return "ahead", v
    if has_behind:
        return "behind", v
    return "none", v


def _balance(snap: Snapshot, step: int) -> Answer:
    bias = snap.front_brake_bias
    if bias <= 0:
        return "no_bias", {}
    return "default", {"bias": str(bias), "bias_to": str(bias + step)}


ANSWERS: Mapping[str, Callable[[Snapshot], Answer]] = {
    "tyres": _tyres,
    "pit": _pit,
    "gap": _gap,
    "gap_behind": _gap_behind,
    "fuel": _fuel,
    "plan": _plan,
    "rain": _rain,
    "push": _push,
    "race_stat": _race_stat,
    "fight": _fight,
    "understeer": lambda s: _balance(s, -1),  # bias rearward frees the front
    "oversteer": lambda s: _balance(s, +1),  # bias forward calms the rear
}

TEMPLATE_KEYS = frozenset(
    {"lap", "laps_left", "mindset", "wear", "age", "pace_laps", "plan_lap", "in_laps"}
    | {"reason", "gain", "window", "gap", "name", "trend", "margin", "compound_sets"}
    | {
        "in10",
        "in30",
        "to",
        "tyre",
        "bias",
        "bias_to",
        "label",
        "pos",
        "best",
        "ahead",
        "behind",
        "budget",
    }
)


def answer(item: MenuItemModel, snap: Snapshot, mindset: str) -> Answer:
    """Case + template values for a confirmed item. Unknown handlers and
    actions answer "default" with the common values."""
    values = _common(snap, mindset)
    values["label"] = item.label
    handler = ANSWERS.get(item.answer or item.id) if item.kind != "action" else None
    if handler is None:
        return "default", values
    case, extra = handler(snap)
    values.update(extra)
    return case, values


class _Blank(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


class ReplyPicker:
    """Rotates reply variants per (item, case): deterministic across replays."""

    def __init__(self) -> None:
        self._n: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        self._n.clear()

    def pick(self, item: MenuItemModel, case: str, values: dict[str, str]) -> str:
        pool = item.replies.get(case) or item.replies.get("default") or []
        if not pool:
            return ""
        key = (item.id, case)
        n = self._n.get(key, 0)
        self._n[key] = n + 1
        return " ".join(pool[n % len(pool)].format_map(_Blank(values)).split())


def validate(settings: MenuSettings) -> list[str]:
    """Config errors for `pitwall rules check`."""
    errors: list[str] = []
    seen: set[str] = set()
    for item in settings.items:
        where = f"menu item {item.id!r}"
        if item.id in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(item.id)
        if item.kind == "action" and item.action is None:
            errors.append(f"{where}: kind action needs `action`")
        if item.kind != "action" and (item.answer or item.id) not in ANSWERS:
            if not item.replies.get("default"):
                errors.append(f"{where}: no answer handler and no default reply")
        for src in (item.show_when, item.rank_when):
            if src:
                try:
                    Predicate(src)
                except ExprError as e:
                    errors.append(f"{where}: {e}")
        if item.kind == "opinion" and not item.topic:
            errors.append(f"{where}: opinion needs a `topic`")
        for case, pool in item.replies.items():
            for text in pool:
                try:
                    fields = {f for _, f, _, _ in string.Formatter().parse(text) if f}
                except ValueError as e:
                    errors.append(f"{where} [{case}]: {e}")
                    continue
                bad = fields - TEMPLATE_KEYS
                if bad:
                    errors.append(f"{where} [{case}]: unknown placeholder(s) {sorted(bad)}")
    return errors


def validate_shortcuts(inp: InputSettings, settings: MenuSettings) -> list[str]:
    ids = {i.id for i in settings.items}
    return [
        f"input.shortcuts: unknown menu item {sc.item!r}"
        for sc in inp.shortcuts
        if sc.item not in ids
    ]
