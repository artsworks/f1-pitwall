"""Packet-rate detectors for driver-behaviour calls: wheel lock-ups, spins,
boost left on, and yellow flags relative to the player's position.

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
        spot_m: float = 75.0,
    ) -> None:
        self.slip = slip
        self.spot_m = spot_m
        self._spots: list[tuple[int, float]] = []  # (lap, lap distance) of every lock-up
        self.spot_laps = 0
        self.min_speed_kmh = min_speed_kmh
        self.min_brake = min_brake
        self.min_s = min_s
        self.release_s = release_s
        self.hold_s = hold_s
        self.reset()
        self.count_lap = 0
        self.count = 0  # this session
        self._lap = 0

    def reset(self) -> None:
        self._start: float | None = None
        self._last_locked = 0.0
        self._worst = 0.0
        self._worst_wheel = 0
        self._ended_at: float | None = None
        self._start_dist: float | None = None
        self.wheel = -1

    def update(
        self,
        t: float,
        slip: Corners,
        speed_kmh: float,
        brake: float,
        lap: int,
        dist_m: float | None = None,
    ) -> None:
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
                self._start_dist = dist_m
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
                self.count += 1
                self._note_spot(lap, self._start_dist)
            self._start = None

    def _note_spot(self, lap: int, dist_m: float | None) -> None:
        """spot_laps: earlier laps that locked up in the same braking zone."""
        if dist_m is None:
            self.spot_laps = 0
            return
        self.spot_laps = len(
            {lp for lp, d in self._spots if lp != lap and abs(d - dist_m) <= self.spot_m}
        )
        self._spots.append((lap, dist_m))

    def recent(self, t: float) -> tuple[str, str]:
        """('front' | 'rear' | '', wheel name) for a lock-up that ended within hold_s."""
        if self._ended_at is None or self.wheel < 0 or t - self._ended_at > self.hold_s:
            return "", ""
        return ("rear" if self.wheel < 2 else "front"), WHEEL_NAMES[self.wheel]


class SpinDetector:
    """Loss of control followed by a slow recovery: the moment the driver is
    about to rejoin on overheated rears. A slide is sideslip (angle between
    where the car points and where it travels, from Motion Ex local velocity)
    of at least `slide_deg` above `min_speed_kmh` for `min_s`. It is reported
    when the car is travelling straight again below `rejoin_kmh`, and stays
    reported for `hold_s`. A slide caught at speed is not reported."""

    def __init__(
        self,
        *,
        slide_deg: float = 40.0,
        min_speed_kmh: float = 30.0,
        min_s: float = 0.2,
        straight_deg: float = 20.0,
        rejoin_kmh: float = 80.0,
        hold_s: float = 4.0,
    ) -> None:
        self.slide_deg = slide_deg
        self.min_speed_kmh = min_speed_kmh
        self.min_s = min_s
        self.straight_deg = straight_deg
        self.rejoin_kmh = rejoin_kmh
        self.hold_s = hold_s
        self.count = 0
        self.reset()

    def reset(self) -> None:
        self._start: float | None = None
        self._lost = False
        self._at: float | None = None

    def update(self, t: float, local_velocity: tuple[float, float, float]) -> None:
        vx, _, vz = local_velocity
        speed_kmh = math.hypot(vx, vz) * 3.6
        slip_deg = math.degrees(math.atan2(abs(vx), vz)) if speed_kmh > 5 else 0.0
        if speed_kmh >= self.min_speed_kmh and slip_deg >= self.slide_deg:
            if self._start is None:
                self._start = t
            if t - self._start >= self.min_s:
                self._lost = True
            return
        self._start = None
        if self._lost and speed_kmh > 5 and slip_deg < self.straight_deg:
            self._lost = False
            if speed_kmh < self.rejoin_kmh:
                self._at = t
                self.count += 1

    def recent(self, t: float) -> bool:
        return self._at is not None and 0 <= t - self._at <= self.hold_s


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


class SaveDetector:
    """A moment the driver caught: sideslip of at least `save_deg` above
    `min_speed_kmh` that straightens out again (below `straight_deg` for
    `settle_s`) without ever reaching `spin_deg`. Reported for `hold_s`,
    with the peak angle so rules can scale the reaction."""

    def __init__(
        self,
        *,
        save_deg: float = 12.0,
        spin_deg: float = 40.0,
        min_speed_kmh: float = 60.0,
        min_s: float = 0.1,
        straight_deg: float = 5.0,
        settle_s: float = 0.3,
        hold_s: float = 4.0,
    ) -> None:
        self.save_deg = save_deg
        self.spin_deg = spin_deg
        self.min_speed_kmh = min_speed_kmh
        self.min_s = min_s
        self.straight_deg = straight_deg
        self.settle_s = settle_s
        self.hold_s = hold_s
        self.count = 0
        self.peak_deg = 0.0
        self.reset()

    def reset(self) -> None:
        self._start: float | None = None
        self._peak = 0.0
        self._spun = False
        self._straight: float | None = None
        self._at: float | None = None

    def update(self, t: float, local_velocity: tuple[float, float, float]) -> None:
        vx, _, vz = local_velocity
        speed_kmh = math.hypot(vx, vz) * 3.6
        slip_deg = math.degrees(math.atan2(abs(vx), vz)) if speed_kmh > 5 else 0.0
        if self._start is None:
            if speed_kmh >= self.min_speed_kmh and slip_deg >= self.save_deg:
                self._start, self._peak, self._spun, self._straight = t, slip_deg, False, None
            return
        self._peak = max(self._peak, slip_deg)
        if slip_deg >= self.spin_deg or speed_kmh < 5:
            self._spun = True
        if slip_deg >= self.straight_deg:
            self._straight = None
            return
        if self._straight is None:
            self._straight = t
        if t - self._straight < self.settle_s:
            return
        caught = not self._spun and self._straight - self._start >= self.min_s
        if caught and speed_kmh >= self.min_speed_kmh / 2:
            self._at = t
            self.count += 1
            self.peak_deg = self._peak
        self._start = None

    def recent(self, t: float) -> bool:
        return self._at is not None and 0 <= t - self._at <= self.hold_s


OFF_SURFACES = frozenset(range(2, 11))  # concrete run-off, rock, gravel, mud, sand, grass, ...


class OffTrackTracker:
    """Places lost to a trip off the track, and whether they came back on the
    same lap. An excursion is at least `min_wheels` wheels on an off surface
    for `min_s`; once back on track for `settle_s` the position is compared
    with the one before the excursion. `lost` (places) is reported for
    `hold_s`; regaining them before the lap ends reports `recovered`."""

    def __init__(
        self,
        *,
        min_wheels: int = 2,
        min_s: float = 0.3,
        settle_s: float = 5.0,
        hold_s: float = 6.0,
    ) -> None:
        self.min_wheels = min_wheels
        self.min_s = min_s
        self.settle_s = settle_s
        self.hold_s = hold_s
        self.reset()

    def reset(self) -> None:
        self._off_since: float | None = None
        self._before = 0
        self._lap = 0
        self._back_at: float | None = None
        self._owed = 0  # places lost and not yet regained this lap
        self._owed_pos = 0
        self.places = 0
        self._lost_at: float | None = None
        self._recovered_at: float | None = None
        self._position = 0
        self._cur_lap = 0

    def update_position(self, position: int, lap: int, in_pit: bool) -> None:
        if in_pit or lap != self._lap:
            self._owed = 0
        self._position, self._cur_lap = position, lap

    def update_surface(self, t: float, surfaces: tuple[int, ...]) -> None:
        off = sum(1 for s in surfaces if s in OFF_SURFACES) >= self.min_wheels
        if off:
            if self._off_since is None:
                self._off_since = t
                if self._back_at is None:
                    self._before, self._lap = self._position, self._cur_lap
            self._back_at = None
            return
        if self._off_since is not None:
            went_off = t - self._off_since >= self.min_s
            self._off_since = None
            if went_off:
                self._back_at = t
            return
        if self._back_at is not None and t - self._back_at >= self.settle_s:
            self._back_at = None
            lost = self._position - self._before if self._before and self._position else 0
            if lost > 0 and self._cur_lap == self._lap:
                self.places, self._lost_at = lost, t
                self._owed, self._owed_pos = lost, self._before
        if self._owed and self._position and self._position <= self._owed_pos:
            if self._cur_lap == self._lap:
                self._recovered_at = t
            self._owed = 0

    def lost_recent(self, t: float) -> int:
        if self._lost_at is None or not 0 <= t - self._lost_at <= self.hold_s:
            return 0
        return self.places

    def recovered_recent(self, t: float) -> bool:
        return self._recovered_at is not None and 0 <= t - self._recovered_at <= self.hold_s


class ContactTracker:
    """Player collisions (COLL events) grouped into episodes: hits within
    `merge_s` of the last one extend the episode instead of starting a new
    check. The damage readings at the first hit are the baseline the
    post-contact report is measured against."""

    def __init__(self, *, merge_s: float = 8.0) -> None:
        self.merge_s = merge_s
        self.episodes = 0
        self.reset()

    def reset(self) -> None:
        self.start: float | None = None
        self.last = 0.0
        self.other = -1
        self.severity = 0
        self.hits = 0
        self.baseline: dict[str, int] = {}

    def hit(self, t: float, other: int, severity: int, damage: dict[str, int]) -> None:
        if self.start is None or t - self.last > self.merge_s or t < self.start:
            self.start, self.other, self.severity, self.hits = t, other, severity, 0
            self.baseline = dict(damage)
            self.episodes += 1
        self.last = t
        self.hits += 1
        self.severity = max(self.severity, severity)

    def phase(self, t: float, check_s: float, report_s: float) -> str:
        """'checking' right after the hit, 'report' once the damage data has
        settled, '' when there is no recent contact."""
        if self.start is None:
            return ""
        age = t - self.last
        if 0 <= t - self.start and age < check_s:
            return "checking"
        if check_s <= age < check_s + report_s:
            return "report"
        return ""

    def worst_new(self, damage: dict[str, int], min_pct: int) -> tuple[str, int]:
        """(part, percent now) with the biggest rise since the first hit, if
        the rise is at least `min_pct`."""
        part, rise = "", 0
        for name, now in damage.items():
            d = now - self.baseline.get(name, 0)
            if d >= min_pct and d > rise:
                part, rise = name, d
        return part, damage.get(part, 0)
