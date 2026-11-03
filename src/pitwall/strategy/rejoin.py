"""Rejoin-position prediction for the pit-window optimiser (docs/18): where a
stop after k more laps drops us back into the field. Pure and deterministic
like the optimiser that feeds it."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FieldCar:
    name: str
    gap_s: float  # signed race-time gap to us, penalties included; negative = ahead
    pace_ms: int
    deg_ms_per_lap: float
    pitted: bool


@dataclass(frozen=True, slots=True)
class Rejoin:
    position: int
    ahead_name: str  # car directly ahead at rejoin; "" when P1
    gap_to_ahead_s: float  # inf when P1
    in_traffic: bool


def rejoin_position(
    field: Sequence[FieldCar],
    *,
    k: int,
    our_lap_ms: Callable[[int], float],
    pit_loss_s: float,
    warmup_s: float,
    dirty_air_gap_s: float,
) -> Rejoin:
    """Projected position after running k more non-stop laps then pitting.

    A rival's race time at our rejoin is `gap_s` plus its k projected laps;
    ours is our k laps plus the pit loss and out-lap warm-up. A rival is
    strictly ahead when its total is lower. `gap_to_ahead_s` is our margin to
    the closest car ahead; `in_traffic` means it lies inside the dirty-air gap.
    Rivals' own future stops are not modelled inside the horizon.
    """
    our_ms = sum(our_lap_ms(j) for j in range(k)) + (pit_loss_s + warmup_s) * 1000.0
    ahead_count = 0
    gap_to_ahead_s = math.inf
    ahead_name = ""
    for car in field:
        if car.pace_ms <= 0:
            continue
        rival_ms = car.gap_s * 1000.0 + sum(car.pace_ms + car.deg_ms_per_lap * j for j in range(k))
        diff_s = (rival_ms - our_ms) / 1000.0
        if diff_s < 0.0:
            ahead_count += 1
            if -diff_s < gap_to_ahead_s:
                gap_to_ahead_s = -diff_s
                ahead_name = car.name
    return Rejoin(1 + ahead_count, ahead_name, gap_to_ahead_s, gap_to_ahead_s < dirty_air_gap_s)
