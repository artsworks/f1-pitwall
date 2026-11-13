"""Track zone lookahead and rival overtake state, read from SessionState."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pitwall.state.session import SessionState


def drs_zone_ahead(state: SessionState) -> bool:
    """A legacy DRS zone starts within the lookahead distance."""
    return zone_ahead(
        state,
        state._drs_zones,
        state._th("drs_zone_lookahead_m", 300.0),
    )


def aero_zone_ahead(state: SessionState) -> bool:
    if state.regulations_2026:
        distance = state.active_aero_activation_distance_m
        return 0 < distance <= state._th("aero_zone_lookahead_m", 250.0)
    return zone_ahead(
        state,
        state._aero_zones,
        state._th("aero_zone_lookahead_m", 250.0),
    )


def overtake_zone_ahead(state: SessionState) -> bool:
    distance = state.overtake_activation_distance_m
    return 0 < distance <= state._th("overtake_zone_lookahead_m", 300.0)


def zone_ahead(state: SessionState, zones: Sequence[Any], lookahead_m: float) -> bool:
    if state.track_length_m <= 0:
        return False
    for zone in zones:
        dist = (float(zone.zone_start) - state.lap_distance) % state.track_length_m
        if 0 < dist <= lookahead_m:
            return True
    return False


def rival_overtake_active(state: SessionState, index: int) -> bool:
    if index < 0 or state.cars_telemetry_2 is None:
        return False
    try:
        return bool(state.cars_telemetry_2[index].overtake_active)
    except IndexError:
        return False
