"""Measured pit loss and its priors (docs/18). loss_ms = (in_lap - ref) +
(out_lap - ref); lane time lives inside the lap times already."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

from pitwall.model.deg import Prior, resolve_prior

if TYPE_CHECKING:
    from pitwall.config.models import TrackOverlay
    from pitwall.store.db import Database, LapRow

# sc_status max over in/out laps -> (param suffix, overlay key, threshold key).
_KEYS = {
    0: ("green", "green", "pit_loss_default_s"),
    1: ("sc", "sc", "pit_loss_sc_default_s"),
    2: ("vsc", "vsc", "pit_loss_vsc_default_s"),
}


@dataclass(frozen=True, slots=True)
class PitLoss:
    loss_ms: int
    lane_ms: int
    in_lap_ms: int
    out_lap_ms: int
    ref_pace_ms: int
    neutralised: int


def measure(
    in_lap: LapRow,
    out_lap: LapRow,
    lane_ms: int,
    ref_pace_ms: int,
    neutralised: int,
) -> PitLoss:
    loss = (in_lap.lap_time_ms - ref_pace_ms) + (out_lap.lap_time_ms - ref_pace_ms)
    return PitLoss(
        loss_ms=int(loss),
        lane_ms=lane_ms,
        in_lap_ms=in_lap.lap_time_ms,
        out_lap_ms=out_lap.lap_time_ms,
        ref_pace_ms=ref_pace_ms,
        neutralised=neutralised,
    )


def ref_pace_ms(laps: list[LapRow], before_lap_num: int) -> int:
    """Median of the last 3 valid laps before `before_lap_num`; 0 if none."""
    times = [
        lap.lap_time_ms
        for lap in laps
        if lap.lap_num < before_lap_num and lap.valid == 1 and lap.lap_time_ms > 0
    ]
    return int(median(times[-3:])) if times else 0


def _th(th: Mapping[str, object], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default


def current_pit_loss(
    db: Database | None,
    session_uid: int | None,
    track_id: int,
    neutralised: int,
    overlay: TrackOverlay | None,
    th: Mapping[str, object],
) -> Prior:
    """This session's measured events -> learned param -> overlay -> default.

    Value is in milliseconds."""
    suffix, overlay_key, th_key = _KEYS.get(neutralised, _KEYS[0])
    default_ms = _th(th, th_key, 22.0) * 1000.0
    if db is not None and session_uid is not None:
        events = [
            e.loss_ms
            for e in db.pit_events_for_session(session_uid)
            if e.neutralised == neutralised
        ]
        if events:
            return Prior(float(median(events)), float(len(events)), "session")
    overlay_ms: float | None = None
    if overlay is not None:
        v = overlay.pit_loss_s.get(overlay_key)
        if v is not None:
            overlay_ms = v * 1000.0
    return resolve_prior(
        db,
        track_id,
        0,
        f"pit_loss_{suffix}_ms",
        overlay_value=overlay_ms,
        default=default_ms,
        min_weight=_th(th, "prior_min_weight", 2.0),
    )
