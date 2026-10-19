"""Driver-ability rating from clean laps and session results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median, pstdev
from typing import TYPE_CHECKING

from pitwall.config.thresholds import threshold
from pitwall.protocol.enums import session_kind

if TYPE_CHECKING:
    from pitwall.store.db import ModelParam


DRIVER_COMPOUND = 0
GLOBAL_TRACK_ID = -2
PARAM_NAMES = {
    "pace_delta_ms": "driver_pace_delta_ms",
    "consistency_cv": "driver_consistency_cv",
    "deg_delta_ms_per_lap": "driver_deg_delta_ms_per_lap",
    "errors_per_lap": "driver_errors_per_lap",
    "contact_per_lap": "driver_contact_per_lap",
    "position_delta": "driver_position_delta",
}
RATING = "driver_rating"


@dataclass(frozen=True, slots=True)
class DriverInputs:
    lap_times_ms: tuple[int, ...]
    field_lap_times_ms: tuple[int, ...]
    base_ms: float | None
    deg_ms_per_lap: float | None
    deg_prior_ms_per_lap: float | None
    laps: int
    lockups: int
    spins: int
    offs: int
    contacts: int
    grid_position: int
    finish_position: int


@dataclass(frozen=True, slots=True)
class DriverProfile:
    pace_delta_ms: float | None
    consistency_cv: float | None
    deg_delta_ms_per_lap: float | None
    errors_per_lap: float | None
    contact_per_lap: float | None
    position_delta: float | None
    scores: tuple[tuple[str, float], ...]
    rating: float


@dataclass(frozen=True, slots=True)
class DriverScale:
    confidence_factor: float = 1.0
    risk_add: float = 0.0
    pace_cap_ms: float = 0.0


NEUTRAL = DriverScale()


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def _positive_threshold(th: Mapping[str, object], name: str, default: float) -> float:
    return max(threshold(th, name, default), 1e-9)


def component_score(component: str, value: float, th: Mapping[str, object]) -> float:
    if component == "pace_delta_ms":
        score = 0.5 - value / (2 * _positive_threshold(th, "driver_pace_span_ms", 1500))
    elif component == "consistency_cv":
        score = 1.0 - value / _positive_threshold(th, "driver_cv_max", 0.02)
    elif component == "deg_delta_ms_per_lap":
        score = 0.5 - value / (2 * _positive_threshold(th, "driver_deg_span_ms", 60))
    elif component == "errors_per_lap":
        score = 1.0 - value / _positive_threshold(th, "driver_errors_max", 0.5)
    elif component == "contact_per_lap":
        score = 1.0 - value / _positive_threshold(th, "driver_contact_max", 0.2)
    elif component == "position_delta":
        score = 0.5 + value / (2 * _positive_threshold(th, "driver_position_span", 10))
    else:
        raise ValueError(f"unknown driver rating component {component!r}")
    return _clamp(score)


def compute_profile(inp: DriverInputs, th: Mapping[str, object]) -> DriverProfile | None:
    if len(inp.lap_times_ms) < threshold(th, "driver_min_laps", 3) or inp.laps <= 0:
        return None

    pace_delta_ms = None
    if len(inp.field_lap_times_ms) >= threshold(th, "driver_field_min_cars", 3):
        pace_delta_ms = float(median(inp.lap_times_ms) - median(inp.field_lap_times_ms))
    elif inp.base_ms is not None:
        pace_delta_ms = float(median(inp.lap_times_ms) - inp.base_ms)

    mean_lap_ms = sum(inp.lap_times_ms) / len(inp.lap_times_ms)
    consistency_cv = pstdev(inp.lap_times_ms) / mean_lap_ms if len(inp.lap_times_ms) >= 3 else None
    deg_delta_ms_per_lap = (
        inp.deg_ms_per_lap - inp.deg_prior_ms_per_lap
        if inp.deg_ms_per_lap is not None and inp.deg_prior_ms_per_lap is not None
        else None
    )
    errors_per_lap = (inp.lockups + inp.spins + inp.offs) / inp.laps
    contact_per_lap = inp.contacts / inp.laps
    position_delta = (
        float(inp.grid_position - inp.finish_position)
        if inp.grid_position > 0 and inp.finish_position > 0
        else None
    )

    components = (
        ("pace_delta_ms", pace_delta_ms),
        ("consistency_cv", consistency_cv),
        ("deg_delta_ms_per_lap", deg_delta_ms_per_lap),
        ("errors_per_lap", errors_per_lap),
        ("contact_per_lap", contact_per_lap),
        ("position_delta", position_delta),
    )
    scores = tuple(
        (component, component_score(component, value, th))
        for component, value in components
        if value is not None
    )
    rating = sum(score for _, score in scores) / len(scores) if scores else 0.5
    return DriverProfile(
        pace_delta_ms=pace_delta_ms,
        consistency_cv=consistency_cv,
        deg_delta_ms_per_lap=deg_delta_ms_per_lap,
        errors_per_lap=errors_per_lap,
        contact_per_lap=contact_per_lap,
        position_delta=position_delta,
        scores=scores,
        rating=rating,
    )


def fold_weight(session_type: int, th: Mapping[str, object]) -> float:
    kind = session_kind(session_type)
    if kind == "qualifying":
        return threshold(th, "driver_fold_weight_quali", 1)
    if kind == "race":
        return threshold(th, "driver_fold_weight_race", 5)
    return 0.0


def confidence(weight: float, th: Mapping[str, object]) -> float:
    weight = max(0.0, weight)
    k = _positive_threshold(th, "driver_confidence_k", 10)
    return weight / (weight + k)


def advice_scale(rating: float, confidence_value: float, th: Mapping[str, object]) -> DriverScale:
    neutral = threshold(th, "driver_rating_neutral", 0.5)
    deficit = _clamp((neutral - rating) / max(neutral, 1e-9))
    eff = deficit * _clamp(confidence_value)
    low_factor = threshold(th, "driver_low_confidence_factor", 0.7)
    return DriverScale(
        confidence_factor=1.0 - eff * (1.0 - low_factor),
        risk_add=eff * threshold(th, "driver_risk_add", 0.2),
        pace_cap_ms=eff * threshold(th, "driver_pace_cap_ms", 300),
    )


def learned_rating(
    params_track: Sequence[ModelParam],
    params_global: Sequence[ModelParam],
    th: Mapping[str, object],
) -> tuple[float, float, str]:
    min_weight = threshold(th, "driver_min_weight", 1)
    track_param = next(
        (
            param
            for param in params_track
            if param.compound == DRIVER_COMPOUND and param.name == RATING
        ),
        None,
    )
    if track_param is not None and track_param.weight >= min_weight:
        return track_param.value, confidence(track_param.weight, th), "track"

    global_param = next(
        (
            param
            for param in params_global
            if param.compound == DRIVER_COMPOUND and param.name == RATING
        ),
        None,
    )
    if global_param is not None and global_param.weight >= min_weight:
        return global_param.value, confidence(global_param.weight, th), "global"
    return 0.5, 0.0, ""
