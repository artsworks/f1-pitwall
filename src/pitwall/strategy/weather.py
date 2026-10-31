"""Wet-weather compound crossover math (docs/03 "Weather").

Pure functions; SessionState wires them to thresholds and field pace.
"""

from __future__ import annotations

from collections.abc import Mapping

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


def crossover_projection(
    gain_prev_s: float, gain_now_s: float, rate_min_s: float
) -> tuple[float, float, float] | None:
    """(gap_s, rate_s, laps) until the target compound's lap time crosses ours, else None.

    gain = target compound minus current compound advantage per lap (+ = target quicker).
    """
    gap = -gain_now_s
    rate = gain_now_s - gain_prev_s
    if gap <= 0 or rate <= 0 or rate < rate_min_s:
        return None
    return round(gap, 2), round(rate, 2), round(gap / rate, 1)
