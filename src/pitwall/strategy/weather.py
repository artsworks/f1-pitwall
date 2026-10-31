"""Wet-weather compound crossover math (docs/03 "Weather").

Pure functions; SessionState wires them to thresholds and field pace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pitwall.config.thresholds import threshold


def default_pit_loss_s(th: Mapping[str, object], safety_car_status: int) -> float:
    """Default pit loss for the current neutralisation state (s)."""
    if safety_car_status == 1:
        return threshold(th, "pit_loss_sc_default_s", 8.0)
    if safety_car_status == 2:
        return threshold(th, "pit_loss_vsc_default_s", 12.0)
    return threshold(th, "pit_loss_default_s", 22.0)


def switch_net_s(
    gain_per_lap_s: float,
    laps_after: int,
    pit_loss_s: float,
    th: Mapping[str, object],
) -> float | None:
    """Projected net time gain of a compound switch (s), else None.

    A call needs a per-lap gain over the noise floor and enough laps after
    the in-lap to pay the stop back; the caller treats net > 0 as "switch".
    """
    if gain_per_lap_s < threshold(th, "compound_crossover_s", 1.0):
        return None
    if laps_after < threshold(th, "wet_crossover_min_laps", 3.0):
        return None
    return gain_per_lap_s * laps_after - pit_loss_s - threshold(th, "wet_crossover_margin_s", 3.0)


def wet_by_lap(
    samples: Sequence[tuple[int, int]], lap_s: float, lap_frac: float, laps: int
) -> list[float]:
    """Cumulative probability the track is wet by the end of each lap."""
    if not samples:
        return [0.0] * max(0, laps)
    ordered = sorted(samples)
    probabilities: list[float] = []
    running_max = 0.0
    for lap in range(max(0, laps)):
        minute = (lap + 1 - lap_frac) * lap_s / 60.0
        if minute <= ordered[0][0]:
            probability = ordered[0][1] / 100.0
        else:
            probability = ordered[-1][1] / 100.0
            for (start_min, start_pct), (end_min, end_pct) in zip(
                ordered, ordered[1:], strict=False
            ):
                if minute <= end_min:
                    fraction = (minute - start_min) / (end_min - start_min)
                    probability = (start_pct + (end_pct - start_pct) * fraction) / 100.0
                    break
        running_max = max(running_max, probability)
        probabilities.append(running_max)
    return probabilities


def dry_to_wet_cost_s(
    p_wet: Sequence[float],
    stop_after: int | None,
    pit_loss_s: float,
    slick_wet_s: float,
    inter_dry_s: float,
) -> float:
    """Expected time lost against a dry race on slicks."""
    laps = len(p_wet)
    if not laps:
        return 0.0
    total = 0.0
    previous = 0.0
    for lap, probability in enumerate(p_wet):
        onset_probability = probability - previous
        previous = probability
        if stop_after is None or lap <= stop_after:
            cost = min(slick_wet_s + pit_loss_s, slick_wet_s * (laps - lap))
        else:
            cost = pit_loss_s + (lap - stop_after - 1) * inter_dry_s
        total += onset_probability * cost
    no_rain_cost = (
        0.0
        if stop_after is None
        else pit_loss_s + min((laps - 1 - stop_after) * inter_dry_s, pit_loss_s)
    )
    return total + (1.0 - p_wet[-1]) * no_rain_cost


def best_rain_stop(
    p_wet: Sequence[float], pit_loss_s: float, slick_wet_s: float, inter_dry_s: float
) -> tuple[int, float]:
    """Return the best pre-emptive stop lap and gain over staying out."""
    laps = len(p_wet)
    if not laps:
        return -1, 0.0
    stay_out = dry_to_wet_cost_s(p_wet, None, pit_loss_s, slick_wet_s, inter_dry_s)
    best_stop = -1
    best_cost = stay_out
    for stop_after in range(laps - 1):
        cost = dry_to_wet_cost_s(p_wet, stop_after, pit_loss_s, slick_wet_s, inter_dry_s)
        if cost < best_cost:
            best_stop = stop_after
            best_cost = cost
    return best_stop, stay_out - best_cost
