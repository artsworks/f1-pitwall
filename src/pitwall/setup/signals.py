"""Run-level setup signals derived from persisted lap data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Any

from pitwall.setup.states import runs_for_session
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


def session_signals(
    db: Database,
    uid: int,
    thresholds: Mapping[str, Any],
    *,
    learned_slip_base: float | None = None,
) -> RunSignals | None:
    """Build signals from the latest run and all green laps on its setup state."""
    runs = runs_for_session(db, uid)
    if not runs:
        return None
    session = db.session_row(uid)
    if session is None:
        return None

    last_run = runs[-1]
    run_laps = [lap for lap in last_run.laps if _green(lap)]
    all_green = [lap for lap in db.laps_for(uid) if _green(lap)]
    event_laps = [lap for lap in all_green if lap.setup_state_id == last_run.setup_state_id]
    event_count = len(event_laps)
    minimum_event_laps = int(thresholds.get("setup_min_event_laps", 3))

    def rate(field: str) -> float | None:
        if event_count < minimum_event_laps:
            return None
        return 10.0 * sum(int(getattr(lap, field) or 0) for lap in event_laps) / event_count

    entry_snaps = sum(lap.snaps_entry for lap in event_laps)
    exit_snaps = sum(lap.snaps_exit for lap in event_laps)
    if entry_snaps > exit_snaps:
        snap_phase = "entry"
    elif exit_snaps > entry_snaps:
        snap_phase = "exit"
    else:
        snap_phase = ""

    state_slip, state_samples = _weighted_slip(event_laps)
    slip_base: float | None
    if learned_slip_base is not None:
        slip_base = learned_slip_base
    else:
        sampled_states = {
            lap.setup_state_id
            for lap in all_green
            if lap.setup_state_id is not None and lap.slip_samples > 0
        }
        all_slip, _ = _weighted_slip(all_green)
        slip_base = all_slip if len(sampled_states) >= 2 else None
    slip_balance = (
        state_slip - slip_base
        if state_slip is not None and state_samples > 0 and slip_base is not None
        else None
    )

    front_rate = _slope(run_laps, "wear_front_pct")
    rear_rate = _slope(run_laps, "wear_rear_pct")
    wear_axle_ratio = (
        rear_rate / front_rate
        if (
            len(run_laps) >= 2
            and front_rate is not None
            and front_rate > 0
            and rear_rate is not None
        )
        else None
    )

    compound = last_run.compound
    return RunSignals(
        session_uid=uid,
        track_id=int(session["track_id"]),
        session_type=int(session["session_type"]),
        compound=compound,
        setup_state_id=last_run.setup_state_id,
        run_laps=len(run_laps),
        event_laps=event_count,
        traction_exits_per10=rate("traction_exits"),
        lockups_rear_per10=rate("lockups_rear"),
        lockups_front_per10=rate("lockups_front"),
        snaps_entry_per10=rate("snaps_entry"),
        snaps_exit_per10=rate("snaps_exit"),
        snap_phase=snap_phase,
        slip_balance=slip_balance,
        wear_axle_ratio=wear_axle_ratio,
        z_front=_z_score(run_laps, "tyre_inner_front_c", compound, thresholds),
        z_rear=_z_score(run_laps, "tyre_inner_rear_c", compound, thresholds),
    )
