"""Packet-rate detectors for driver-behaviour calls: wheel lock-ups, boost
left on, and yellow flags relative to the player's position.

Each runs on session_time (game clock) and exposes plain values the Snapshot
copies; rules decide what is worth saying.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pitwall.protocol.layouts import Corners

WHEEL_NAMES = ("rear left", "rear right", "front left", "front right")  # Corners wire order


class LockupDetector:
    """A lock-up is any wheel with slip ratio <= `slip` while braking above
    `min_speed_kmh`, sustained for `min_s`. Reported once the episode ends,
    so the call lands after the corner entry rather than during it."""

    def __init__(
        self,
        *,
        slip: float = -0.3,
        min_speed_kmh: float = 50.0,
        min_brake: float = 0.1,
        min_s: float = 0.25,
        release_s: float = 0.1,
        hold_s: float = 3.0,
    ) -> None:
        self.slip = slip
        self.min_speed_kmh = min_speed_kmh
        self.min_brake = min_brake
        self.min_s = min_s
        self.release_s = release_s
        self.hold_s = hold_s
        self.reset()
        self.count_lap = 0
        self._lap = 0

    def reset(self) -> None:
        self._start: float | None = None
        self._last_locked = 0.0
        self._worst = 0.0
        self._worst_wheel = 0
        self._ended_at: float | None = None
        self.wheel = -1

    def update(self, t: float, slip: Corners, speed_kmh: float, brake: float, lap: int) -> None:
        if lap != self._lap:
            self._lap = lap
            self.count_lap = 0
        values = slip.as_tuple()
        idx = min(range(4), key=lambda i: values[i])
        locked = (
            values[idx] <= self.slip and speed_kmh >= self.min_speed_kmh and brake >= self.min_brake
        )
        if locked:
            if self._start is None:
                self._start = t
                self._worst = values[idx]
                self._worst_wheel = idx
            elif values[idx] < self._worst:
                self._worst = values[idx]
                self._worst_wheel = idx
            self._last_locked = t
            return
        if self._start is not None and t - self._last_locked >= self.release_s:
            if self._last_locked - self._start >= self.min_s:
                self._ended_at = self._last_locked
                self.wheel = self._worst_wheel
                self.count_lap += 1
            self._start = None

    def recent(self, t: float) -> tuple[str, str]:
        """('front' | 'rear' | '', wheel name) for a lock-up that ended within hold_s."""
        if self._ended_at is None or self.wheel < 0 or t - self._ended_at > self.hold_s:
            return "", ""
        return ("rear" if self.wheel < 2 else "front"), WHEEL_NAMES[self.wheel]


class BoostTimer:
    """Seconds the ERS deploy mode has continuously been `boost` (3)."""

    BOOST = 3

    def __init__(self) -> None:
        self._since: float | None = None
        self._t = 0.0

    def reset(self) -> None:
        self._since = None

    def update(self, t: float, deploy_mode: int) -> None:
        self._t = t
        if deploy_mode == self.BOOST:
            if self._since is None:
                self._since = t
        else:
            self._since = None

    def on_s(self, t: float) -> float:
        return 0.0 if self._since is None else max(0.0, t - self._since)


YELLOW = 3


@dataclass(slots=True)
class _Zone:
    start_m: float
    end_m: float
    yellow: bool = False
    own: bool = False  # went yellow with the player inside it: likely the player's own incident
    behind_at_onset: bool = False


@dataclass(frozen=True, slots=True)
class YellowView:
    ahead_m: float = math.inf  # distance to the nearest yellow the player is driving towards
    ahead_sector: int = 0  # 1-based sector of that yellow
    behind_m: float = math.inf  # distance back to a yellow that appeared behind the player
    behind_sector: int = 0
    here: bool = False


class YellowTracker:
    """Marshal-zone yellows (Session packet, 2 Hz) against the player's lap
    distance. A zone is 'ahead' when the way forward to it is shorter than the
    way back. Zones that turn yellow while the player is inside them are
    ignored: in live data they are almost always the player's own off."""

    def __init__(self) -> None:
        self.zones: list[_Zone] = []
        self.track_m = 0.0
        self.s2_m = 0.0
        self.s3_m = 0.0

    def reset(self) -> None:
        self.zones = []

    def update(
        self,
        starts: list[float],
        flags: list[int],
        track_m: float,
        s2_m: float,
        s3_m: float,
        car_m: float,
    ) -> None:
        if track_m <= 0 or not starts:
            return
        self.track_m, self.s2_m, self.s3_m = track_m, s2_m, s3_m
        bounds = [s * track_m for s in starts] + [track_m]
        if len(self.zones) != len(starts):
            self.zones = [_Zone(bounds[i], bounds[i + 1]) for i in range(len(starts))]
        for z, flag in zip(self.zones, flags, strict=True):
            yellow = flag == YELLOW
            if yellow and not z.yellow:
                z.own = self._inside(z, car_m)
                fwd, back = self._fwd_back(z, car_m)
                z.behind_at_onset = back < fwd
            z.yellow = yellow

    def _inside(self, z: _Zone, car_m: float) -> bool:
        return z.start_m <= car_m < z.end_m

    def _fwd_back(self, z: _Zone, car_m: float) -> tuple[float, float]:
        L = self.track_m
        return (z.start_m - car_m) % L, (car_m - z.end_m) % L

    def _sector(self, m: float) -> int:
        if self.s3_m and m >= self.s3_m:
            return 3
        if self.s2_m and m >= self.s2_m:
            return 2
        return 1

    def view(self, car_m: float) -> YellowView:
        ahead_m = behind_m = math.inf
        ahead_sector = behind_sector = 0
        here = False
        for z in self.zones:
            if not z.yellow or z.own:
                continue
            if self._inside(z, car_m):
                here = True
                continue
            fwd, back = self._fwd_back(z, car_m)
            if fwd <= back:
                if fwd < ahead_m:
                    ahead_m, ahead_sector = fwd, self._sector(z.start_m)
            elif z.behind_at_onset and back < behind_m:
                behind_m, behind_sector = back, self._sector(z.start_m)
        return YellowView(ahead_m, ahead_sector, behind_m, behind_sector, here)
