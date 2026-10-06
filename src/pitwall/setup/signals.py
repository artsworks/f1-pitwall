"""Run-level setup signals derived from persisted lap data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from statistics import fmean
from typing import Any

from pitwall.setup.states import Run, runs_for_session
from pitwall.state.session import thermal_window
from pitwall.store.db import Database, LapRow


@dataclass(frozen=True, slots=True)
class RunSignals:
    session_uid: int
    track_id: int
    session_type: int
    compound: int
    setup_state_id: int | None
    run_laps: int
    event_laps: int
    traction_exits_per10: float | None
    lockups_rear_per10: float | None
    lockups_front_per10: float | None
    snaps_entry_per10: float | None
    snaps_exit_per10: float | None
    snap_phase: str
    slip_balance: float | None
    wear_axle_ratio: float | None
    z_front: float | None
    z_rear: float | None
    slip_raw: float | None = None
    lap_slope_ms: float | None = None
    wear_front_slope: float | None = None
    wear_rear_slope: float | None = None


def _green(lap: LapRow) -> bool:
    return bool(lap.valid) and lap.sc_status == 0


def _weighted_slip(rows: Sequence[LapRow]) -> tuple[float | None, int]:
    samples = sum(lap.slip_samples for lap in rows if lap.slip_samples > 0)
    if samples == 0:
        return None, 0
    total = sum(lap.slip_balance_deg * lap.slip_samples for lap in rows if lap.slip_samples > 0)
    return total / samples, samples


def _slope(rows: Sequence[LapRow], field: str) -> float | None:
    if len(rows) < 2:
        return None
    xs = [float(row.lap_num) for row in rows]
    ys = [float(getattr(row, field)) for row in rows]
    mean_x, mean_y = fmean(xs), fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator


def _lap_slope_ms(rows: Sequence[LapRow]) -> float | None:
    timed = [row for row in rows if row.lap_time_ms > 0]
    return _slope_x(timed, "lap_time_ms", "tyre_age_laps")


def _slope_x(rows: Sequence[LapRow], y_field: str, x_field: str) -> float | None:
    if len(rows) < 2:
        return None
    xs = [float(getattr(row, x_field)) for row in rows]
    ys = [float(getattr(row, y_field)) for row in rows]
    mean_x, mean_y = fmean(xs), fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator


def _z_score(
    rows: Sequence[LapRow],
    field: str,
    compound: int,
    thresholds: Mapping[str, Any],
) -> float | None:
    values = [float(getattr(row, field)) for row in rows if getattr(row, field) > 0]
    if not values:
        return None
    cold, hot = thermal_window(thresholds, compound)
    half_width = (hot - cold) / 2.0
    if half_width <= 0:
        return None
    return (fmean(values) - (cold + hot) / 2.0) / half_width


def _rate(rows: Sequence[LapRow], field: str, minimum_laps: int) -> float | None:
    if len(rows) < minimum_laps:
        return None
    return 10.0 * sum(int(getattr(lap, field) or 0) for lap in rows) / len(rows)


def _snap_phase(rows: Sequence[LapRow]) -> str:
    entry = sum(lap.snaps_entry for lap in rows)
    exit = sum(lap.snaps_exit for lap in rows)
    if entry > exit:
        return "entry"
    if exit > entry:
        return "exit"
    return ""


def slip_base_for_session(
    db: Database,
    uid: int,
    compound: int,
    thresholds: Mapping[str, Any],
    learned_slip_base: float | None = None,
) -> float | None:
    if learned_slip_base is not None:
        return learned_slip_base
    session = db.session_row(uid)
    if session is None:
        return None
    learned = db.get_param(int(session["track_id"]), compound, "setup_base:slip_balance_deg")
    minimum_weight = thresholds.get("setup_base_min_weight", 1.0)
    if (
        learned is not None
        and isinstance(minimum_weight, int | float)
        and learned.weight >= float(minimum_weight)
    ):
        return learned.value
    all_green = [lap for lap in db.laps_for(uid) if _green(lap)]
    sampled_states = {
        lap.setup_state_id
        for lap in all_green
        if lap.setup_state_id is not None and lap.slip_samples > 0
    }
    all_slip, _ = _weighted_slip(all_green)
    return all_slip if len(sampled_states) >= 2 else None


def signals_for_run(
    db: Database,
    uid: int,
    run: Run,
    thresholds: Mapping[str, Any],
    *,
    learned_slip_base: float | None = None,
) -> RunSignals:
    """Build setup signals from the green laps in one run."""
    session = db.session_row(uid)
    if session is None:
        raise ValueError(f"session {uid} was not found")
    run_laps = [lap for lap in run.laps if _green(lap)]
    minimum_event_laps = int(thresholds.get("setup_min_event_laps", 3))
    slip_raw, slip_samples = _weighted_slip(run_laps)
    slip_balance = (
        slip_raw - learned_slip_base
        if slip_raw is not None and slip_samples > 0 and learned_slip_base is not None
        else None
    )

    front_rate = _slope(run_laps, "wear_front_pct")
    rear_rate = _slope(run_laps, "wear_rear_pct")
    wear_axle_ratio = (
        rear_rate / front_rate
        if (front_rate is not None and front_rate > 0 and rear_rate is not None)
        else None
    )

    compound = run.compound
    return RunSignals(
        session_uid=uid,
        track_id=int(session["track_id"]),
        session_type=int(session["session_type"]),
        compound=compound,
        setup_state_id=run.setup_state_id,
        run_laps=len(run_laps),
        event_laps=len(run_laps),
        traction_exits_per10=_rate(run_laps, "traction_exits", minimum_event_laps),
        lockups_rear_per10=_rate(run_laps, "lockups_rear", minimum_event_laps),
        lockups_front_per10=_rate(run_laps, "lockups_front", minimum_event_laps),
        snaps_entry_per10=_rate(run_laps, "snaps_entry", minimum_event_laps),
        snaps_exit_per10=_rate(run_laps, "snaps_exit", minimum_event_laps),
        snap_phase=_snap_phase(run_laps),
        slip_balance=slip_balance,
        wear_axle_ratio=wear_axle_ratio,
        z_front=_z_score(run_laps, "tyre_inner_front_c", compound, thresholds),
        z_rear=_z_score(run_laps, "tyre_inner_rear_c", compound, thresholds),
        slip_raw=slip_raw,
        lap_slope_ms=_lap_slope_ms(run_laps),
        wear_front_slope=front_rate,
        wear_rear_slope=rear_rate,
    )


def session_signals(
    db: Database,
    uid: int,
    thresholds: Mapping[str, Any],
    *,
    learned_slip_base: float | None = None,
) -> RunSignals | None:
    """Build last-run signals while keeping rates scoped to its setup state."""
    runs = runs_for_session(db, uid)
    if not runs:
        return None
    session = db.session_row(uid)
    if session is None:
        return None
    last_run = runs[-1]
    all_green = [lap for lap in db.laps_for(uid) if _green(lap)]
    event_laps = [lap for lap in all_green if lap.setup_state_id == last_run.setup_state_id]
    slip_base = slip_base_for_session(db, uid, last_run.compound, thresholds, learned_slip_base)

    signals = signals_for_run(
        db,
        uid,
        last_run,
        thresholds,
        learned_slip_base=slip_base,
    )
    state_slip, state_samples = _weighted_slip(event_laps)
    state_slip_balance = (
        state_slip - slip_base
        if state_slip is not None and state_samples > 0 and slip_base is not None
        else None
    )
    minimum_event_laps = int(thresholds.get("setup_min_event_laps", 3))
    return replace(
        signals,
        event_laps=len(event_laps),
        traction_exits_per10=_rate(event_laps, "traction_exits", minimum_event_laps),
        lockups_rear_per10=_rate(event_laps, "lockups_rear", minimum_event_laps),
        lockups_front_per10=_rate(event_laps, "lockups_front", minimum_event_laps),
        snaps_entry_per10=_rate(event_laps, "snaps_entry", minimum_event_laps),
        snaps_exit_per10=_rate(event_laps, "snaps_exit", minimum_event_laps),
        snap_phase=_snap_phase(event_laps),
        slip_balance=state_slip_balance,
    )
