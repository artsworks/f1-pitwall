"""Race phase machine and rival scope (docs/18 "pitwall.state.race").

Phase graph:

    formation → racing → sc | vsc → racing …
    racing → in_lap (pit_status != 0) → out_lap (pit_status back to 0,
      until the next lap boundary) → racing
    any → finished (result_status FINISHED, or CHQF then the line crossed)

SC/VSC exit is hysteresis'd on session time so a status flicker doesn't
bounce the phase.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


class RacePhase:
    """Race-session phase machine. `phase` is the current state; `sc_laps`
    counts laps completed under the ongoing SC/VSC period."""

    def __init__(self) -> None:
        self.phase = "formation"
        self.sc_laps = 0
        self._finished = False
        self._sc_kind = ""  # 'sc' | 'vsc' while inside a neutralised period
        self._sc_last_seen = 0.0
        self._ever_raced = False

    def update(
        self,
        *,
        session_time: float,
        safety_car_status: int,
        pit_status: int,
        driver_status: int,
        result_status: int,
        lap_num: int,
        lap_boundary: bool,
        lights_out_seen: bool,
        chequered_seen: bool,
        red_flag: bool,
        sc_exit_hold_s: float,
    ) -> str:
        if not self._finished and (
            result_status == 3 or (chequered_seen and lap_boundary)
        ):  # FINISHED, or flag then the line
            self._finished = True
        if self._finished:
            self.phase = "finished"
            self._sc_kind = ""
            self.sc_laps = 0
            return "red_flag" if red_flag else self.phase

        phase = self.phase

        # Neutralisation: enter immediately, leave only after the hold.
        if safety_car_status in (1, 2):
            self._sc_kind = "sc" if safety_car_status == 1 else "vsc"
            self._sc_last_seen = session_time
            phase = self._sc_kind
        elif self._sc_kind and session_time - self._sc_last_seen < sc_exit_hold_s:
            phase = self._sc_kind  # hold so a flicker doesn't bounce
        else:
            self._sc_kind = ""
            if self.phase in ("sc", "vsc", "formation", "racing"):
                # Pit sequence only applies from the free-running phases.
                if self.phase == "formation":
                    if not (
                        safety_car_status == 3
                        or (lap_num == 0 and not lights_out_seen and not self._ever_raced)
                    ):
                        phase = "racing"
                        self._ever_raced = True
                    else:
                        phase = "formation"
                else:
                    phase = "racing"
            elif self.phase == "in_lap":
                phase = "out_lap" if pit_status == 0 else "in_lap"
            elif self.phase == "out_lap":
                if pit_status != 0:
                    phase = "in_lap"
                elif lap_boundary:
                    phase = "racing"
            if self.phase in ("racing", "sc", "vsc") and pit_status != 0:
                phase = "in_lap"
            if phase == "racing" and driver_status == 2:
                phase = "in_lap"
            elif phase == "racing" and driver_status == 3:
                phase = "out_lap"
        if self._sc_kind:
            self._ever_raced = self._ever_raced or self.phase in ("racing", "in_lap", "out_lap")

        if lap_boundary:
            self.sc_laps = self.sc_laps + 1 if phase in ("sc", "vsc") else 0
        self.phase = phase
        return "red_flag" if red_flag else phase


def relevant_rivals(
    cars: Sequence[Any],
    player_idx: int,
    gap_behind_max_s: float,
    pit_exit_projection: tuple[float, float, float],
) -> tuple[int, int, int]:
    """(ahead, behind, pit_exit) car indices; -1 = none.

    ahead/behind are the cars one race position either side of the player
    (behind only if its gap to the player is within gap_behind_max_s).
    pit_exit is the car whose lap distance is nearest ahead of
    D_target = (D_player - L * T_pit / P) mod L among cars not pitting —
    pit_exit_projection carries (D_player, L, L*T_pit/P) i.e. the metres a
    stop is projected to cost.
    """
    ahead = behind = pit_exit = -1
    if not cars or not (0 <= player_idx < len(cars)):
        return ahead, behind, pit_exit
    player = cars[player_idx]
    pos = getattr(player, "car_position", 0)
    for i, c in enumerate(cars):
        if i == player_idx:
            continue
        if getattr(c, "result_status", 0) != 2:  # ACTIVE
            continue
        if getattr(c, "car_position", 0) == pos - 1 and pos > 1:
            ahead = i
        elif getattr(c, "car_position", 0) == pos + 1:
            behind = i
    if behind >= 0:
        gap_ms = getattr(cars[behind], "delta_to_car_in_front_ms", 0)
        if gap_ms <= 0 or gap_ms / 1000.0 > gap_behind_max_s:
            behind = -1

    d_player, track_m, metres_lost = pit_exit_projection
    if track_m > 0:
        d_target = (d_player - metres_lost) % track_m
        best = math.inf
        for i, c in enumerate(cars):
            if i == player_idx or getattr(c, "pit_status", 0) != 0:
                continue
            if getattr(c, "result_status", 0) != 2:
                continue
            fwd = (c.lap_distance - d_target) % track_m
            if fwd < best:
                best = fwd
                pit_exit = i
    return ahead, behind, pit_exit
