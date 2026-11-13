"""Weather tracking for SessionState: forecast crossover, the dry-to-wet stop,
the field's switches to wet tyres and the wet-to-dry confirmation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pitwall.strategy.plans import DRY
from pitwall.strategy.weather import best_rain_stop, default_pit_loss_s, wet_by_lap

if TYPE_CHECKING:
    from pitwall.protocol.packets import CarStatusPacket
    from pitwall.state.session import SessionState


def race_horizon_min(state: SessionState) -> float:
    """Minutes of racing left, so forecast samples after the flag don't
    drive a tyre call. 30 (the forecast's reach) when unknown."""
    lap_ms = state._best_laps.get(state._player_idx, 0)
    if state._kind() != "race" or state.total_laps <= 0 or lap_ms <= 0:
        return 30.0
    laps_left = max(0, state.total_laps - state.lap_num + 1)
    return laps_left * lap_ms / 60_000.0


def update_weather_crossover(state: SessionState) -> None:
    """Crossover direction from the forecast, with % hysteresis so it
    doesn't flap around the thresholds. The reported minute is the first
    forecast sample that crosses the band, not the wettest one."""
    horizon = race_horizon_min(state)
    ahead = [
        (int(f.rain_percentage), int(f.time_offset))
        for f in state._forecast_samples
        if f.session_type == state.session_type
        and 5 <= f.time_offset
        and (f.time_offset <= horizon or f.time_offset <= 10)
    ]
    rain_heads_up = min(
        (
            (pct, offset)
            for pct, offset in ahead
            if offset <= horizon
            and offset <= state._th("rain_heads_up_max_min", 15.0)
            and pct >= state._th("rain_heads_up_pct", 40.0)
        ),
        key=lambda sample: sample[1],
        default=(0, 0),
    )
    state._rain_soon_pct, state._rain_soon_min = rain_heads_up
    rain, rain_min = max(ahead, default=(0, 0))
    state._weather_crossover_pct, state._weather_crossover_min = rain, rain_min
    wet = state._th("rain_wet_pct", 85.0)
    inter = state._th("rain_inter_pct", 60.0)
    dry = state._th("rain_dry_pct", 30.0)
    hyst = state._th("rain_hysteresis_pct", 5.0)
    cur = state._weather_crossover
    # Wetter states need the forecast past the band edge; clearing a
    # state needs it back below the band edge (hysteresis).
    if rain >= wet + (0.0 if cur == "to_inter" else hyst):
        cand = "to_wet"
    elif rain >= inter + (0.0 if cur == "to_wet" else hyst):
        cand = "to_inter"
    elif (state.weather >= 3 or state.tyre_compound in (7, 8)) and rain < dry - (
        0.0 if cur else hyst
    ):
        cand = "to_dry"
    elif cur in ("to_inter", "to_wet") and rain < inter - hyst:
        cand = ""
    else:
        cand = cur
    if cand in ("to_inter", "to_wet"):
        edge = wet if cand == "to_wet" else inter
        first = min(
            ((pct, m) for pct, m in ahead if pct >= edge),
            key=lambda p: p[1],
            default=None,
        )
        if first is not None:
            state._weather_crossover_pct, state._weather_crossover_min = first
    state._weather_crossover = cand


def update_dry_to_wet(state: SessionState) -> None:
    samples = [
        sample for sample in state._forecast_samples if sample.session_type == state.session_type
    ]
    state.rain_onset_min = (
        0
        if state.weather >= 3
        else min(
            (int(sample.time_offset) for sample in samples if sample.weather >= 3),
            default=0,
        )
    )
    state.rain_box_lap = -1
    state.rain_box_tyre = ""
    state.rain_box_gain_s = 0.0
    laps_remaining = max(0, state.total_laps - state.lap_num + 1) if state.total_laps else 0
    best_lap_ms = state._best_laps.get(state._player_idx, 0)
    if (
        state._kind() != "race"
        or state.tyre_visual in (7, 8)
        or state.weather >= 3
        or laps_remaining <= 1
        or not samples
        or best_lap_ms <= 0
    ):
        return
    lap_s = best_lap_ms / 1000.0
    lap_frac = (
        min(1.0, max(0.0, state.lap_distance / state.track_length_m))
        if state.track_length_m > 0
        else 0.0
    )
    probabilities = wet_by_lap(
        [(int(sample.time_offset), int(sample.rain_percentage)) for sample in samples],
        lap_s,
        lap_frac,
        laps_remaining,
    )
    stop_lap, gain = best_rain_stop(
        probabilities,
        default_pit_loss_s(state._thresholds, state.safety_car_status),
        lap_s * state._th("slick_wet_loss_pct", 15.0) / 100.0,
        lap_s * state._th("inter_dry_loss_pct", 6.0) / 100.0,
    )
    if stop_lap >= 0 and gain >= state._th("rain_preempt_margin_s", 2.0):
        state.rain_box_lap = stop_lap
        max_rain_pct = max(int(sample.rain_percentage) for sample in samples)
        state.rain_box_tyre = (
            "wets" if max_rain_pct >= state._th("rain_wet_pct", 85.0) else "inters"
        )
        state.rain_box_gain_s = round(gain, 1)


def update_field_wet_switches(state: SessionState, pkt: CarStatusPacket | None = None) -> None:
    if pkt is not None:
        for index, car in enumerate(pkt.cars):
            compound = int(car.visual_tyre_compound)
            previous = state._field_previous_visual_compounds.get(index)
            if index != state._player_idx and previous in DRY and compound in (7, 8):
                state._field_wet_switch_laps[index] = (state.lap_num, compound)
            state._field_previous_visual_compounds[index] = compound

    window = max(0, int(state._th("field_follow_window_laps", 3)))
    counted_compounds = [
        switch_compound
        for index, (switch_lap, switch_compound) in state._field_wet_switch_laps.items()
        if index != state._player_idx
        and state._field_previous_visual_compounds.get(index) in (7, 8)
        and 0 <= state.lap_num - switch_lap < window
    ]
    state.field_wet_switches = len(counted_compounds)
    wet_count = counted_compounds.count(8)
    inter_count = counted_compounds.count(7)
    state.field_wet_tyre = (
        "wets" if wet_count > inter_count else "inters" if counted_compounds else ""
    )


def weather_at(state: SessionState, offset_min: int) -> int:
    """Forecast weather type of the nearest sample at time_offset >= offset_min."""
    return next(
        (
            int(s.weather)
            for s in state._forecast_samples
            if s.session_type == state.session_type and s.time_offset >= offset_min
        ),
        -1,
    )


def rain_at(state: SessionState, offset_min: int) -> int:
    """Rain chance % of the nearest forecast sample for this session type at
    time_offset >= offset_min (0 = current conditions sample)."""
    return next(
        (
            int(s.rain_percentage)
            for s in state._forecast_samples
            if s.session_type == state.session_type and s.time_offset >= offset_min
        ),
        0,
    )


def wet_to_dry_confirmed(state: SessionState, to: str) -> bool:
    """The direction's net test passed for wet_to_dry_confirm_laps in a
    row; SC laps simply aren't entries, so confirmation carries through."""
    need = int(state._th("wet_to_dry_confirm_laps", 2.0))
    entries = [
        passes for lap, passes in sorted(state._wet_to_dry_passes.items()) if lap <= state.lap_num
    ][-need:]
    return len(entries) >= need and all(to in passes for passes in entries)
