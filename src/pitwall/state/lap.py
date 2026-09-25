"""Lap accumulator and LapSummary. Validity per docs/03 "Lap validity":
not lap 1; pit_status == 0 throughout; not the lap after an in-lap;
safety_car_status == 0 throughout; current_lap_invalid == 0; no flashback.
(The docs' "no weather transition" needs Session weather tracking — deferred,
recorded but not yet an invalidator.)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LapSummary:
    lap_num: int
    lap_time_ms: int
    sector1_ms: int
    sector2_ms: int
    compound: int
    tyre_age_laps: int
    fuel_remaining_laps_at_end: float
    valid: bool
    invalid_reasons: list[str] = field(default_factory=list)


class LapAccumulator:
    """Tracks the player's in-progress lap; emits a LapSummary when
    current_lap_num increments."""

    def __init__(self) -> None:
        self._cur_lap = 0
        self._prev_was_in_lap = False
        self._saw_pit = False
        self._saw_sc = False
        self._saw_flashback = False
        self._saw_invalid = False
        self._last_driver_status = 0

    def note_flashback(self) -> None:
        self._saw_flashback = True

    def reset_stint_flags(self) -> None:
        self._saw_pit = self._saw_sc = self._saw_flashback = self._saw_invalid = False

    def update(
        self,
        *,
        current_lap_num: int,
        last_lap_time_ms: int,
        sector1_ms: int,
        sector2_ms: int,
        pit_status: int,
        driver_status: int,
        current_lap_invalid: int,
        safety_car_status: int,
        compound: int,
        tyre_age_laps: int,
        fuel_remaining_laps: float,
    ) -> LapSummary | None:
        """Feed one Lap Data tick for the player. Returns a summary when the
        lap counter increments."""
        if current_lap_num == self._cur_lap and current_lap_num != 0:
            self._accumulate(pit_status, current_lap_invalid, safety_car_status)
            self._last_driver_status = driver_status
            return None

        # Lap boundary.
        finished = self._cur_lap
        self._cur_lap = current_lap_num
        prev_flags = (
            self._saw_pit,
            self._saw_sc,
            self._saw_flashback,
            self._saw_invalid,
            self._prev_was_in_lap,
        )
        self.reset_stint_flags()
        self._prev_was_in_lap = driver_status == 2  # in lap
        self._accumulate(pit_status, current_lap_invalid, safety_car_status)
        self._last_driver_status = driver_status

        if finished == 0:
            return None  # first observation; nothing completed
        saw_pit, saw_sc, saw_fb, saw_inv, prev_in_lap = prev_flags
        reasons: list[str] = []
        if finished == 1:
            reasons.append("first_lap")
        if saw_pit:
            reasons.append("pitted")
        if prev_in_lap:
            reasons.append("after_in_lap")
        if saw_sc:
            reasons.append("safety_car")
        if saw_inv:
            reasons.append("invalid")
        if saw_fb:
            reasons.append("flashback")
        return LapSummary(
            lap_num=finished,
            lap_time_ms=last_lap_time_ms,
            sector1_ms=sector1_ms,
            sector2_ms=sector2_ms,
            compound=compound,
            tyre_age_laps=tyre_age_laps,
            fuel_remaining_laps_at_end=fuel_remaining_laps,
            valid=not reasons,
            invalid_reasons=reasons,
        )

    def _accumulate(self, pit_status: int, invalid: int, sc: int) -> None:
        self._saw_pit |= pit_status != 0
        self._saw_sc |= sc != 0
        self._saw_invalid |= invalid != 0
