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

GAP_TOLERANCE_S = 3.0


def gap_plausible(gap_ms: int, dist_m: float, speed_mps: float) -> bool:
    """True if a game time gap agrees with the on-track distance between the two cars."""
    if gap_ms <= 0:
        return False
    if speed_mps <= 0:
        return True
    est = max(0.0, dist_m) / speed_mps
    return abs(gap_ms / 1000.0 - est) <= max(GAP_TOLERANCE_S, 0.5 * est)


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
        self._pit_seen = False  # pit lane entered during this in_lap
        self._last_driver_status = -1
        self._red_restart = False
        self.neutral_end_kind = ""  # 'sc' | 'vsc' of the last neutralisation that ended
        self.neutral_end_t = -math.inf

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

        if red_flag:
            # The red flag supersedes any SC/VSC; the restart is a fresh start.
            self._sc_kind = ""
            self.sc_laps = 0
            self._red_restart = True
            return "red_flag"

        phase = self.phase

        # Neutralisation: enter immediately, leave only after the hold.
        if safety_car_status in (1, 2):
            self._sc_kind = "sc" if safety_car_status == 1 else "vsc"
            self._sc_last_seen = session_time
            phase = self._sc_kind
        elif self._sc_kind and session_time - self._sc_last_seen < sc_exit_hold_s:
            phase = self._sc_kind  # hold so a flicker doesn't bounce
        else:
            if self._sc_kind:
                self.neutral_end_kind = self._sc_kind
                self.neutral_end_t = session_time
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
                if pit_status != 0:
                    self._pit_seen = True
                elif self._pit_seen:
                    phase = "out_lap"
                elif lap_boundary:
                    phase = "racing"  # box requested but never came in
            elif self.phase == "out_lap":
                if pit_status != 0:
                    phase = "in_lap"
                    self._pit_seen = True
                elif lap_boundary:
                    phase = "racing"
            if self.phase in ("racing", "sc", "vsc") and pit_status != 0:
                phase = "in_lap"
                self._pit_seen = True
            # The game can hold driver_status at IN_LAP for the rest of the race,
            # so only the transition into it starts an in-lap.
            fresh_in_lap = driver_status == 2 and self._last_driver_status != 2
            if phase == "racing" and fresh_in_lap:
                phase = "in_lap"
                self._pit_seen = False
            elif (
                phase == "racing"
                and driver_status == 3
                and self._last_driver_status != 3
                and not self._red_restart  # the restart grid reports OUT_LAP too
            ):
                phase = "out_lap"  # e.g. a pit-lane start
        self._last_driver_status = driver_status
        if self._sc_kind:
            self._ever_raced = self._ever_raced or self.phase in ("racing", "in_lap", "out_lap")

        if lap_boundary:
            self._red_restart = False
            self.sc_laps = self.sc_laps + 1 if phase in ("sc", "vsc") else 0
        self.phase = phase
        return "red_flag" if red_flag else phase


def relevant_rivals(
    cars: Sequence[Any],
    player_idx: int,
    gap_behind_max_s: float,
    pit_exit_projection: tuple[float, float, float],
    ref_speed_mps: float = 0.0,
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
    track_m = pit_exit_projection[1]
    for i, c in enumerate(cars):
        if i == player_idx:
            continue
        if getattr(c, "result_status", 0) != 2:  # ACTIVE
            continue
        if getattr(c, "car_position", 0) == pos - 1 and pos > 1:
            ahead = i
        elif getattr(c, "car_position", 0) == pos + 1:
            behind = i
    if ahead >= 0:
        candidate = cars[ahead]
        player_distance = getattr(player, "total_distance", None)
        candidate_distance = getattr(candidate, "total_distance", None)
        if getattr(candidate, "pit_status", 0) != 0 or (
            player_distance is not None
            and candidate_distance is not None
            and not gap_plausible(
                getattr(player, "delta_to_car_in_front_ms", 0),
                candidate_distance - player_distance,
                ref_speed_mps,
            )
        ):
            ahead = -1
    if behind >= 0:
        candidate = cars[behind]
        gap_ms = getattr(candidate, "delta_to_car_in_front_ms", 0)
        if gap_ms <= 0 or gap_ms / 1000.0 > gap_behind_max_s:
            behind = -1
        elif getattr(candidate, "pit_status", 0) != 0:
            behind = -1
        else:
            player_distance = getattr(player, "total_distance", None)
            candidate_distance = getattr(candidate, "total_distance", None)
            if player_distance is not None and candidate_distance is not None:
                distance = player_distance - candidate_distance
                if (track_m > 0 and distance >= track_m) or not gap_plausible(
                    gap_ms, distance, ref_speed_mps
                ):
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


def penalty_standing(
    cars: Sequence[Any], player_idx: int, track_m: float, speed_mps: float = 0.0
) -> tuple[int, float, int]:
    """(position once time penalties are applied, margin in seconds over the
    first car on the road behind that the penalties bring closest, its index).

    Race time is each car's own delta_to_race_leader plus its penalty
    seconds, so one car's glitched timing cannot shift everyone behind it.
    A car a full lap (track_m) or more behind on total distance is lapped
    and cannot take a place on time.
    A negative margin means that car finishes ahead of the player.
    """
    if not (0 <= player_idx < len(cars)):
        return 0, math.inf, -1
    me = cars[player_idx]
    pos = getattr(me, "car_position", 0)
    if pos <= 0:
        return 0, math.inf, -1
    road = sorted(
        (
            (c.car_position, i)
            for i, c in enumerate(cars)
            if getattr(c, "car_position", 0) > 0 and getattr(c, "result_status", 0) in (2, 3)
        )
    )
    leader = next((cars[i] for p, i in road if p == 1), me)
    total: dict[int, float] = {}
    for p, i in road:
        c = cars[i]
        if p > pos and me.total_distance - c.total_distance >= track_m:
            continue
        if p == 1:
            race_time = 0.0
        else:
            dist = leader.total_distance - c.total_distance
            if gap_plausible(c.delta_to_race_leader_ms, dist, speed_mps):
                race_time = c.delta_to_race_leader_ms / 1000.0
            elif speed_mps > 0:
                race_time = max(0.0, dist) / speed_mps
            else:
                race_time = max(0, c.delta_to_race_leader_ms) / 1000.0
        total[i] = race_time + c.penalties
    if player_idx not in total or not any(cars[i].penalties for i in total):
        return pos, math.inf, -1
    mine = (total[player_idx], pos)
    adj = 1 + sum(
        1 for i, t in total.items() if i != player_idx and (t, cars[i].car_position) < mine
    )
    margin, threat = math.inf, -1
    for i, t in total.items():
        if cars[i].car_position > pos and t - mine[0] < margin:
            margin, threat = t - mine[0], i
    return adj, margin, threat
