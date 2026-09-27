"""Pit-window optimiser (docs/18): single-stop race-time model over a short
horizon, plus undercut/overcut, free-stop and SC/VSC cheap-stop decisions.
Pure and deterministic: every input is a plain value so the decision log can
record it and review can re-run it."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from pitwall.model.deg import DegFit


@dataclass(frozen=True, slots=True)
class RivalView:
    idx: int
    name: str
    pace_ms: int
    pitted: bool


@dataclass(frozen=True, slots=True)
class PitPlan:
    plan: str  # box_now | box_in_n | cheap_stop | free_stop | undercut | overcut | stay | no_stop
    lap: int
    gain_s: float
    confidence: float
    risk: float
    rival_idx: int
    rival_name: str
    reason: str
    window: tuple[int, int]
    undercut_s: float
    overcut_s: float
    projections: tuple[tuple[int, float], ...]


NO_PLAN = PitPlan("", 0, 0.0, 0.0, 0.0, -1, "", "", (0, 0), 0.0, 0.0, ())

PIT_LOSS_CONF = {"session": 1.0, "learned": 1.0, "overlay": 0.8, "default": 0.6}


def _f(src: Mapping[str, object], name: str, default: float) -> float:
    v = src.get(name, default)
    return float(v) if isinstance(v, int | float) else default


def _lap_ms(fit: DegFit, age: int, cliff_age: float, cliff_ms_per_lap: float) -> float:
    over = max(0.0, age - cliff_age)
    return fit.base_ms + fit.deg_ms_per_lap * age + cliff_ms_per_lap * over


def _cliff_age(fit: DegFit, cliff_ms: float) -> float:
    return math.inf if fit.deg_ms_per_lap <= 0 else cliff_ms / fit.deg_ms_per_lap


def race_time_s(
    *,
    stop_after: int | None,
    laps_remaining: int,
    tyre_age: int,
    cur: DegFit,
    fresh: DegFit,
    cur_cliff_age: float,
    pit_loss_s: float,
    th: Mapping[str, object],
) -> float:
    """Seconds to finish when stopping after `stop_after` more laps (None = never)."""
    cliff_pen = _f(th, "cliff_ms_per_lap", 1000)
    fresh_cliff = _cliff_age(fresh, _f(th, "tyre_cliff_ms", 1500))
    total = 0.0
    for j in range(laps_remaining):
        if stop_after is None or j < stop_after:
            total += _lap_ms(cur, tyre_age + j, cur_cliff_age, cliff_pen)
        else:
            total += _lap_ms(fresh, j - stop_after, fresh_cliff, cliff_pen)
    if stop_after is not None:
        total += pit_loss_s * 1000.0 + _f(th, "outlap_warmup_s", 0.8) * 1000.0
    return total / 1000.0


def optimise(
    *,
    lap_num: int,
    laps_remaining: int,
    tyre_age: int,
    wear_mean: float,
    fit: DegFit,
    fresh: DegFit,
    laps_of_pace: float,
    pit_loss_s: float,
    pit_loss_source: str,
    green_pit_loss_s: float,
    sc_status: int,
    rival_ahead: RivalView | None,
    rival_behind: RivalView | None,
    gap_ahead_s: float,
    gap_behind_s: float,
    pit_exit_clean: bool,
    restricted: bool,
    mode: Mapping[str, object],
    th: Mapping[str, object],
) -> PitPlan:
    if laps_remaining <= 0 or pit_loss_s <= 0:
        return NO_PLAN
    horizon = int(_f(th, "pit_horizon_laps", 8))
    min_left = int(_f(th, "pit_min_laps_left", 2))
    # The fresh stint uses the current fit's base (compound priors differ in
    # absolute pace from one car to the next) with the fresh prior's slope.
    fresh_fit = DegFit(
        fit.base_ms, fresh.deg_ms_per_lap, fit.fuel_ms_per_lap, 0, 0.0, fresh.confidence, "prior"
    )
    cur_cliff = tyre_age + laps_of_pace
    last_k = max(0, min(horizon, laps_remaining - min_left))

    def total(k: int | None) -> float:
        return race_time_s(
            stop_after=k,
            laps_remaining=laps_remaining,
            tyre_age=tyre_age,
            cur=fit,
            fresh=fresh_fit,
            cur_cliff_age=cur_cliff,
            pit_loss_s=pit_loss_s,
            th=th,
        )

    no_stop = total(None)
    projections = (
        tuple((lap_num + k, round(total(k) - no_stop, 2)) for k in range(last_k + 1))
        if laps_remaining > min_left
        else ()
    )
    conf = fit.confidence * PIT_LOSS_CONF.get(pit_loss_source, 0.6)
    rival_conf = conf * (_f(th, "restricted_confidence_factor", 0.7) if restricted else 1.0)
    # A stop the tyres force anyway costs the place whenever it is taken.
    stop_forced = laps_of_pace < laps_remaining - _f(mode, "tyre_life_buffer_laps", 2.0)
    risk = (
        0.0
        if stop_forced or (not math.isfinite(gap_behind_s) and gap_behind_s > 0)
        else min(max((pit_loss_s - gap_behind_s) / pit_loss_s, 0.0), 1.0)
    )
    gain_min = _f(mode, "pit_gain_min_s", 1.0)

    # Undercut: our fresh laps vs the rival's current (degrading) pace.
    uc_laps = int(_f(th, "undercut_laps", 2))
    undercut_s = 0.0
    if rival_ahead is not None and rival_ahead.pace_ms > 0 and math.isfinite(gap_ahead_s):
        gained = 0.0
        for j in range(uc_laps):
            theirs = rival_ahead.pace_ms + fit.deg_ms_per_lap * (j + 1)
            ours = fresh_fit.base_ms + fresh_fit.deg_ms_per_lap * j
            gained += (theirs - ours) / 1000.0
        undercut_s = round(gained - _f(th, "outlap_warmup_s", 0.8) - gap_ahead_s, 2)
    # Overcut: rival just pitted; our old laps vs their warm-up + fresh laps.
    oc_laps = int(_f(th, "overcut_laps", 2))
    overcut_s = 0.0
    if rival_ahead is not None and rival_ahead.pitted:
        lost = 0.0
        for j in range(oc_laps):
            lost += (
                _lap_ms(fit, tyre_age + j, cur_cliff, _f(th, "cliff_ms_per_lap", 1000))
                - (fresh_fit.base_ms + fresh_fit.deg_ms_per_lap * j)
            ) / 1000.0
        overcut_s = round(_f(th, "outlap_warmup_s", 0.8) - lost, 2)

    if not projections:
        return PitPlan(
            "no_stop",
            0,
            0.0,
            conf,
            0.0,
            -1,
            "",
            "too late to stop",
            (0, 0),
            undercut_s,
            overcut_s,
            (),
        )
    best_k, best_delta = min(
        ((k, d) for k, (_, d) in enumerate(projections)), key=lambda kd: (kd[1], kd[0])
    )
    window_laps = [lap for lap, d in projections if d - best_delta <= gain_min]
    window = (min(window_laps), max(window_laps))
    ahead_idx = rival_ahead.idx if rival_ahead is not None else -1
    ahead_name = rival_ahead.name if rival_ahead is not None else ""

    def plan(
        name: str,
        lap: int,
        gain: float,
        reason: str,
        idx: int = -1,
        who: str = "",
        confidence: float | None = None,
    ) -> PitPlan:
        if confidence is None:
            confidence = rival_conf if idx >= 0 else conf
        return PitPlan(
            name,
            lap,
            round(gain, 2),
            round(confidence, 3),
            round(risk, 3),
            idx,
            who,
            reason,
            window,
            undercut_s,
            overcut_s,
            projections,
        )

    if (
        sc_status != 0
        and wear_mean >= _f(mode, "sc_stop_min_wear_pct", 35)
        and laps_remaining > _f(th, "sc_stop_min_laps_left", 3)
    ):
        # Measured wear plus the neutralised pit loss decide this, not the deg fit.
        return plan(
            "cheap_stop",
            lap_num,
            green_pit_loss_s - pit_loss_s,
            "Cheap stop under the safety car",
            confidence=PIT_LOSS_CONF.get(pit_loss_source, 0.6),
        )
    # Staying out beats every stop inside the horizon and the tyre lasts.
    if best_delta >= 0 and laps_of_pace >= laps_remaining:
        return plan("no_stop", 0, -best_delta, "Tyres make the end")
    if (
        rival_ahead is not None
        and gap_ahead_s < _f(th, "undercut_max_gap_s", 3.0)
        and undercut_s >= _f(mode, "undercut_speak_threshold_s", 1.0)
        and window[0] <= lap_num
    ):
        return plan(
            "undercut", lap_num, undercut_s, f"Undercut on {ahead_name}", ahead_idx, ahead_name
        )
    if (
        rival_ahead is not None
        and rival_ahead.pitted
        and laps_of_pace >= _f(th, "overcut_min_laps", 3)
        and overcut_s >= gain_min
    ):
        return plan(
            "overcut",
            lap_num + oc_laps,
            overcut_s,
            f"Overcut on {ahead_name}",
            ahead_idx,
            ahead_name,
        )
    # Gain of the best lap over its neighbour: stopping one lap later, or now.
    later = projections[best_k + 1][1] if best_k + 1 < len(projections) else best_delta
    delay_gain = (later if best_k == 0 else projections[0][1]) - best_delta
    if (
        best_k <= 1
        and gap_behind_s > pit_loss_s + _f(th, "free_stop_margin_s", 1.0)
        and pit_exit_clean
    ):
        return plan("free_stop", lap_num, max(delay_gain, gain_min), "Free stop, gap behind")
    if best_k == 0:
        reason = "Tyres are done" if laps_of_pace < 1 else "This is the lap"
        return plan("box_now", lap_num, delay_gain, reason)
    if best_k <= _f(th, "box_in_max_laps", 3):
        return plan("box_in_n", lap_num + best_k, delay_gain, "Window open soon")
    return plan("stay", lap_num + best_k, delay_gain, "Window later")
