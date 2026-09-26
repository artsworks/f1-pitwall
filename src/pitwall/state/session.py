"""SessionState: single mutable model updated per packet; Snapshot is the
frozen view rules read. Player car only for M1 (24-slot arrays kept)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pitwall.protocol.enums import DriverStatus, PitStatus, SessionType, VisualCompound
from pitwall.protocol.header import PacketHeader, PacketId
from pitwall.protocol.layouts import CAR_SLOTS, Corners
from pitwall.protocol.packets import (
    CarDamagePacket,
    CarSetupsPacket,
    CarStatusPacket,
    CarTelemetry2Packet,
    CarTelemetryPacket,
    EventPacket,
    LapDataPacket,
    MotionExPacket,
    ParticipantsPacket,
    SessionHistoryPacket,
    SessionPacket,
    TyreSetsPacket,
    parse,
)
from pitwall.state.driving import (
    WHEEL_NAMES,
    BoostTimer,
    LockupDetector,
    SpinDetector,
    YellowTracker,
)
from pitwall.state.ema import CornersEma
from pitwall.state.lap import LapAccumulator, LapSummary
from pitwall.state.pressure import PressureCall, RunTemps, pressure_advice, pressure_text
from pitwall.state.quali import (
    ReleaseWindow,
    abort_advice,
    projected_lap_ms,
    quali_cutoff_ms,
    quali_margin_ms,
    release_window,
)
from pitwall.state.runplan import COOL, HotLap, Plan, RunTracker, mistakes_text, run_plan

PACKET_NAMES: dict[int, str] = {
    PacketId.SESSION: "session",
    PacketId.LAP_DATA: "lap_data",
    PacketId.EVENT: "event",
    PacketId.PARTICIPANTS: "participants",
    PacketId.CAR_SETUPS: "car_setups",
    PacketId.CAR_TELEMETRY: "car_telemetry",
    PacketId.CAR_STATUS: "car_status",
    PacketId.CAR_DAMAGE: "car_damage",
    PacketId.SESSION_HISTORY: "session_history",
    PacketId.TYRE_SETS: "tyre_sets",
    PacketId.MOTION_EX: "motion_ex",
    PacketId.CAR_TELEMETRY_2: "car_telemetry_2",
}

_ZERO_CORNERS = Corners(0.0, 0.0, 0.0, 0.0)

# m_lapValidBitFlags on session-history laps.
LAP_VALID = 0x01
SECTOR1_VALID = 0x02
SECTOR2_VALID = 0x04
SECTOR3_VALID = 0x08


@dataclass(frozen=True, slots=True)
class Damage:
    """Player car damage (percent unless a fault flag)."""

    front_left_wing: int = 0
    front_right_wing: int = 0
    rear_wing: int = 0
    floor: int = 0
    diffuser: int = 0
    sidepod: int = 0
    gearbox: int = 0
    engine: int = 0
    drs_fault: int = 0
    ers_fault: int = 0


_ZERO_DAMAGE = Damage()


@dataclass(frozen=True, slots=True)
class CarLap:
    """Per-car lap data for one tick (all 24 cars)."""

    lap_distance: float = 0.0
    current_lap_time_ms: int = 0
    sector: int = 0
    sector1_ms: int = 0
    sector2_ms: int = 0
    car_position: int = 0
    driver_status: int = 0
    pit_status: int = 0
    result_status: int = 0


@dataclass(frozen=True, slots=True)
class Participant:
    """One entry of the participants list."""

    name: str = ""
    team_id: int = 0
    ai_controlled: int = 0
    race_number: int = 0


@dataclass(frozen=True, slots=True)
class TyreSet:
    """One tyre set from the player's tyre-sets packet."""

    actual: int = 0
    visual: int = 0
    wear: int = 0
    available: int = 0
    life_span: int = 0
    usable_life: int = 0
    fitted: int = 0


def _round50(m: float) -> float:
    return m if math.isinf(m) else round(m / 50) * 50.0


# session_time regression larger than this counts as a flashback/rewind.
REWIND_THRESHOLD_S = 1.0

_PHASE_BY_DRIVER_STATUS = {
    DriverStatus.IN_GARAGE: "garage",
    DriverStatus.FLYING_LAP: "flying",
    DriverStatus.IN_LAP: "in_lap",
    DriverStatus.OUT_LAP: "out_lap",
    DriverStatus.ON_TRACK: "on_track",
}


def _lap_kind(driver_status: int) -> str:
    try:
        return _PHASE_BY_DRIVER_STATUS[DriverStatus(driver_status)]
    except (ValueError, KeyError):
        return ""


@dataclass(slots=True)
class Snapshot:
    """Frozen per-tick view; everything rules read lives here."""

    now: float
    session_time: float = 0.0
    last_packet_t: float | None = None  # clock time of newest packet, same base as `now`
    session_kind: str = "unknown"
    session_type: int = 0
    track_id: int = -1
    total_laps: int = 0
    lap_num: int = 0
    lap_distance: float = 0.0
    sector: int = 0
    position: int = 0
    driver_status: int = 0
    pit_status: int = 0
    phase: str = "garage"
    safety_car_status: int = 0
    tyre_surface: Corners = _ZERO_CORNERS
    tyre_inner: Corners = _ZERO_CORNERS
    brake_temp: Corners = _ZERO_CORNERS
    tyre_surface_ema_fast: Corners = _ZERO_CORNERS
    tyre_surface_ema_slow: Corners = _ZERO_CORNERS
    tyre_inner_ema_fast: Corners = _ZERO_CORNERS
    tyre_inner_ema_slow: Corners = _ZERO_CORNERS
    brake_ema_fast: Corners = _ZERO_CORNERS
    brake_ema_slow: Corners = _ZERO_CORNERS
    tyre_compound: int = 0
    tyre_visual: int = 0
    tyre_age_laps: int = 0
    fuel_remaining_laps: float = 0.0
    fuel_in_tank: float = 0.0
    ers_store_pct: float = 0.0
    ers_deploy_mode: int = 0
    drs_allowed: int = 0
    tyres_wear: Corners = _ZERO_CORNERS
    damage: Damage = _ZERO_DAMAGE
    speed_kmh: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0
    front_brake_bias: int = 0
    tyre_inner_front_c: float = 0.0
    tyre_inner_rear_c: float = 0.0
    coldest_tyre: str = ""
    coldest_tyre_c: float = 0.0
    s3_entry_coldest_c: float = 0.0  # coldest inner temp latched on entering sector 3
    boost_on_s: float = 0.0
    lockup: str = ""  # "front" | "rear" for a few seconds after a lock-up
    lockup_wheel: str = ""
    lockups_this_lap: int = 0
    lockup_spot_laps: int = 0  # earlier laps that locked up in this same braking zone
    spun: bool = False  # for a few seconds after the car spins
    spins: int = 0  # this session
    yellow_here: bool = False
    yellow_ahead_m: float = math.inf
    yellow_ahead_sector: int = 0
    yellow_behind_m: float = math.inf
    yellow_behind_sector: int = 0
    laps: tuple[LapSummary, ...] = ()
    # M2: session context
    session_uid: int = 0
    session_time_left: float = 0.0
    session_duration: float = 0.0
    track_length_m: float = 0.0
    pit_speed_limit: int = 0
    game_paused: bool = False
    paused: bool = False
    num_active_cars: int = 0
    red_flag: bool = False
    session_ended: bool = False
    rewinds: int = 0
    since_rewind_s: float = math.inf
    # M2: per-car lap data (all 24 cars)
    cars: tuple[CarLap, ...] = ()
    # M2: extra player lap fields
    current_lap_time_ms: int = 0
    sector1_time_ms: int = 0
    sector2_time_ms: int = 0
    current_lap_invalid: int = 0
    pit_lane_time_ms: int = 0
    participants: tuple[Participant, ...] = ()
    field_best_laps: tuple[int, ...] = ()
    player_best_lap_ms: int = 0
    player_best_s1_ms: int = 0
    player_best_s2_ms: int = 0
    player_best_s3_ms: int = 0
    player_laps_completed: int = 0
    tyre_sets: tuple[TyreSet, ...] = ()
    fresh_sets_soft: int = 0
    fresh_sets_medium: int = 0
    fresh_sets_hard: int = 0
    fresh_sets_current: int = 0
    fitted_life_span: int = 0
    setup_fuel_load: float = 0.0
    setup_front_wing: int = 0
    setup_rear_wing: int = 0
    setup_brake_bias: int = 0
    active_aero_mode: int = 0
    active_aero_available: int = 0
    overtake_available: int = 0
    overtake_active: int = 0
    overtake_activation_distance_m: int = 0
    driving_wrong_way: bool = False
    # M2 stage 2: qualifying + driver input support
    on_straight: bool = False
    release_gap_ahead_s: float = math.inf
    release_gap_behind_s: float = math.inf
    release_clean: bool = False
    release_wait_s: float = 0.0
    cars_on_track: int = 0
    quali_cutoff_ms: int = 0
    projected_lap_ms: int = 0
    quali_through: bool = False
    abort_deficit_ms: int = 0
    abort_deficit_s: float = 0.0
    abort_advised: bool = False
    abort_reason: str = ""
    # Positive = the field must find this much to take the player's place.
    quali_margin_ms: int = 0
    quali_margin_s: float = 0.0
    quali_margin_kind: str = ""  # "cut" (Q1/Q2) | "pole" (Q3) | "" unknown
    setup_tyre_pressure: Corners = _ZERO_CORNERS
    run_tyre_inner_avg: Corners = _ZERO_CORNERS
    run_flying_s: float = 0.0
    pressure_advice: tuple[PressureCall, ...] = ()
    pressure_advice_text: str = ""
    # Run plan / cool-down lap (docs/17)
    run_lap_kind: str = ""  # "out" | "hot" | "cool" | ""
    line_crossings: int = 0
    run_plan: str = ""  # "push" | "cool" | "box" | "push_now" | ""
    run_plan_reason: str = ""
    run_plan_why: str = ""
    cool_lap: bool = False
    cool_prep: bool = False  # final approach of a cool lap: switch back to hot-lap mode
    cool_elapsed_s: float = 0.0
    ers_need_pct: float = 0.0  # battery wanted at the line before pushing
    cool_extend: bool = False  # cool lap ending short of battery, time for another
    time_for_cool_and_hot: bool = False  # finish this lap slow, then one more hot lap
    dist_to_hot_mode_m: float = 0.0
    last_hot: HotLap | None = None
    last_hot_mistakes: str = ""
    hottest_tyre: str = ""
    hottest_tyre_c: float = 0.0
    cool_tyre_hint: str = ""
    pole_driver: str = ""
    pole_gap_ms: int = 0  # player best - pole best; 0 when unknown or on pole
    pole_gap_s: float = 0.0
    pole_sector_gaps_ms: tuple[int, int, int] = (0, 0, 0)
    pole_worst_sector: int = 0
    pole_worst_sector_s: float = 0.0
    hot_car_behind_s: float = math.inf
    # Nearest on-track car either way: metres, seconds, and its lap kind
    # ("flying" | "out_lap" | "in_lap" | "on_track"). Ahead is timed at the
    # player's push pace; behind at the other car's own speed.
    traffic_ahead_m: float = math.inf
    traffic_ahead_s: float = math.inf
    traffic_ahead_kind: str = ""
    traffic_ahead_closing_s: float = math.inf  # time to catch it at the current speeds
    traffic_ahead_slow: bool = False  # it is much slower than the player right now
    traffic_behind_m: float = math.inf
    traffic_behind_s: float = math.inf
    traffic_behind_kind: str = ""
    dist_to_line_m: float = math.inf
    pit_exit_s: float = math.inf  # since the player left the pit lane
    _ages: dict[str, float] = field(default_factory=dict)

    def age(self, packet_name: str) -> float:
        """Seconds since the named source packet last updated the snapshot."""
        return self._ages.get(packet_name, float("inf"))


class SessionState:
    """Mutable session model. Handlers are registered on Ingest per packet id."""

    def __init__(
        self,
        ema_fast_s: float = 3.0,
        ema_slow_s: float = 30.0,
        *,
        straight_hold_s: float = 1.0,
        press_bit: int | None = None,
        thresholds: Mapping[str, Any] | None = None,
    ) -> None:
        self.last_packet_t: float | None = None
        self.last_recv_wall: float | None = None
        self._player_idx = 0
        self.session_uid: int | None = None
        # Called with session_time on each flashback rewind.
        self.rewind_listeners: list[Callable[[float], None]] = []
        # Called with the new session_uid on each session change.
        self.session_listeners: list[Callable[[int], None]] = []
        # Called with (recv_time, down) on each UDP-action button edge.
        self.press_listeners: list[Callable[[float, bool], None]] = []
        self._ema_fast_s = ema_fast_s
        self._ema_slow_s = ema_slow_s
        self._straight_hold_s = straight_hold_s
        self._press_bit = press_bit
        self._thresholds = dict(thresholds or {})
        self._reset_session()

    def _reset_session(self) -> None:
        """(Re-)initialise all per-session state. Called from __init__ and on
        every session_uid change."""
        self._last_update: dict[str, float] = {}  # packet name -> session_time
        self._last_session_time: float | None = None

        # session context
        self.session_type = 0
        self.track_id = -1
        self.total_laps = 0
        self.safety_car_status = 0
        self.session_time_left = 0.0
        self.session_duration = 0.0
        self.track_length_m = 0.0
        self.pit_speed_limit = 0
        self.game_paused = False
        self.network_paused = False
        self.num_active_cars = 0
        self.red_flag = False
        self.session_ended = False
        self.rewinds = 0
        self._last_rewind_t: float | None = None
        self._was_in_garage = False
        self._pit_exit_t: float | None = None

        # player lap data
        self.lap_num = 0
        self.lap_distance = 0.0
        self.sector = 0
        self.position = 0
        self.driver_status = 0
        self.pit_status = 0
        self.current_lap_time_ms = 0
        self.sector1_time_ms = 0
        self.sector2_time_ms = 0
        self.current_lap_invalid = 0
        self.pit_lane_time_ms = 0

        # telemetry / status / damage (player)
        self.tyre_surface = _ZERO_CORNERS
        self.tyre_inner = _ZERO_CORNERS
        self.brake_temp = _ZERO_CORNERS
        self.run_temps = RunTemps()
        self.setup_tyre_pressure = _ZERO_CORNERS
        self.tyre_compound = 0
        self.tyre_visual = 0
        self.tyre_age_laps = 0
        self.fuel_remaining_laps = 0.0
        self.fuel_in_tank = 0.0
        self.ers_store_pct = 0.0
        self.ers_deploy_mode = 0
        self.drs_allowed = 0
        self.tyres_wear = _ZERO_CORNERS
        self.damage = _ZERO_DAMAGE
        self.speed_kmh = 0.0
        self.throttle = 0.0
        self.brake = 0.0
        self.front_brake_bias = 0
        self.s3_entry_coldest_c = 0.0

        self.lockups = LockupDetector()
        self.spins = SpinDetector()
        self.boost = BoostTimer()
        self.yellows = YellowTracker()

        self.tyre_surface_fast = CornersEma(self._ema_fast_s)
        self.tyre_surface_slow = CornersEma(self._ema_slow_s)
        self.tyre_inner_fast = CornersEma(self._ema_fast_s)
        self.tyre_inner_slow = CornersEma(self._ema_slow_s)
        self.brake_fast = CornersEma(self._ema_fast_s)
        self.brake_slow = CornersEma(self._ema_slow_s)

        self.lap_acc = LapAccumulator()
        self.run = RunTracker()
        self.laps: list[LapSummary] = []

        self.cars_lap: tuple[Any, ...] | None = None
        self.cars_telemetry: tuple[Any, ...] | None = None
        self.cars_status: tuple[Any, ...] | None = None
        self.cars_damage: tuple[Any, ...] | None = None

        # M2 packet state
        self.participants: tuple[Participant, ...] = ()
        self._histories: dict[int, Any] = {}  # car_idx -> SessionHistoryPacket
        self._best_laps: dict[int, int] = {}  # car_idx -> best valid lap_time_ms
        self._best_lap_sectors: dict[int, tuple[int, int, int]] = {}  # sectors of that lap
        self._player_sectors: tuple[int, int, int] = (0, 0, 0)
        self._player_laps_completed = 0
        self._press_down = False
        self._straight_since: float | None = None
        self._tyre_sets: tuple[Any, ...] = ()  # raw TyreSetData objects
        self._tyre_fitted_idx = 0
        self.setup_fuel_load = 0.0
        self.setup_front_wing = 0
        self.setup_rear_wing = 0
        self.setup_brake_bias = 0
        self.active_aero_mode = 0
        self.active_aero_available = 0
        self.overtake_available = 0
        self.overtake_active = 0
        self.overtake_activation_distance_m = 0
        self.driving_wrong_way = False

    # -- ingest entry point -----------------------------------------------

    def register(self, ingest: Any) -> None:
        for pid in PACKET_NAMES:
            ingest.register(pid, self.on_packet)

    def on_packet(self, header: PacketHeader, payload: bytes, recv_time: float = 0.0) -> None:
        pid = header.packet_id
        self._player_idx = header.player_car_index
        self.last_packet_t = header.session_time
        self.last_recv_wall = recv_time
        st = header.session_time
        if self.session_uid is None:
            self.session_uid = header.session_uid
        elif header.session_uid != self.session_uid:
            self._reset_session()
            self.session_uid = header.session_uid
            for cb in self.session_listeners:
                cb(header.session_uid)
        if (
            self._last_session_time is not None
            and st < self._last_session_time - REWIND_THRESHOLD_S
        ):
            self._handle_rewind(st)
        self._last_session_time = st
        name = PACKET_NAMES.get(pid)
        if name is not None:
            self._last_update[name] = st
        try:
            pkt = parse(pid, payload, header)
        except KeyError:
            return
        if isinstance(pkt, SessionPacket):
            self._on_session(pkt)
        elif isinstance(pkt, LapDataPacket):
            self._on_lap_data(pkt)
        elif isinstance(pkt, EventPacket):
            self._on_event(pkt, recv_time)
        elif isinstance(pkt, CarTelemetryPacket):
            self._on_car_telemetry(pkt, st)
        elif isinstance(pkt, CarStatusPacket):
            self._on_car_status(pkt, st)
        elif isinstance(pkt, CarDamagePacket):
            self._on_car_damage(pkt)
        elif isinstance(pkt, ParticipantsPacket):
            self._on_participants(pkt)
        elif isinstance(pkt, CarSetupsPacket):
            self._on_car_setups(pkt)
        elif isinstance(pkt, SessionHistoryPacket):
            self._on_session_history(pkt)
        elif isinstance(pkt, TyreSetsPacket):
            if pkt.car_idx == self._player_idx:
                self._tyre_sets = pkt.sets
                self._tyre_fitted_idx = pkt.fitted_idx
        elif isinstance(pkt, CarTelemetry2Packet):
            self._on_car_telemetry_2(pkt)
        elif isinstance(pkt, MotionExPacket):
            dist = self.lap_distance
            if dist < 0 and self.yellows.track_m:
                dist += self.yellows.track_m
            self.lockups.update(
                st, pkt.wheel_slip_ratio, self.speed_kmh, self.brake, self.lap_num, dist
            )
            self.spins.update(st, pkt.local_velocity)

    def _handle_rewind(self, t: float) -> None:
        self.rewinds += 1
        self._last_rewind_t = t
        for ema in (
            self.tyre_surface_fast,
            self.tyre_surface_slow,
            self.tyre_inner_fast,
            self.tyre_inner_slow,
            self.brake_fast,
            self.brake_slow,
        ):
            ema.reset()
        self.lockups.reset()
        self.spins.reset()
        self.boost.reset()
        self.lap_acc.note_flashback()
        self.run.note_rewind()
        # Per-car session history stays: it is authoritative from the game and
        # is refreshed per car after a rewind. LapData-derived caches reset.
        self.cars_lap = None
        self._straight_since = None
        for cb in self.rewind_listeners:
            cb(t)

    # -- per-packet updates -------------------------------------------------

    def _on_session(self, pkt: SessionPacket) -> None:
        self.session_type = pkt.session_type
        self.track_id = pkt.track_id
        self.total_laps = pkt.total_laps
        self.safety_car_status = pkt.safety_car_status
        self.session_time_left = float(pkt.session_time_left)
        self.session_duration = float(pkt.session_duration)
        self.track_length_m = float(pkt.track_length)
        self.pit_speed_limit = pkt.pit_speed_limit
        self.game_paused = bool(pkt.game_paused)
        zones = pkt.marshal_zones[: pkt.num_marshal_zones]
        self.yellows.update(
            [z.zone_start for z in zones],
            [z.zone_flag for z in zones],
            float(pkt.track_length),
            pkt.sector2_lap_distance_start,
            pkt.sector3_lap_distance_start,
            self.lap_distance,
        )

    def _on_lap_data(self, pkt: LapDataPacket) -> None:
        self.cars_lap = pkt.cars
        car = pkt.cars[self._player_idx]
        self.lap_num = car.current_lap_num
        self.lap_distance = car.lap_distance
        if car.sector == 2 and self.sector != 2:
            self.s3_entry_coldest_c = min(self._ema_or_zero(self.tyre_inner_fast).as_tuple())
        self.sector = car.sector
        self.position = car.car_position
        if car.driver_status == DriverStatus.OUT_LAP and self.driver_status != DriverStatus.OUT_LAP:
            self.run_temps.reset()
        self.driver_status = car.driver_status
        if self.pit_status != PitStatus.NONE and car.pit_status == PitStatus.NONE:
            self._pit_exit_t = self._last_session_time
        self.pit_status = car.pit_status
        self.current_lap_time_ms = car.current_lap_time_ms
        self.sector1_time_ms = car.sector1_ms
        self.sector2_time_ms = car.sector2_ms
        self.current_lap_invalid = car.current_lap_invalid
        self.pit_lane_time_ms = car.pit_lane_time_in_lane_ms
        # Red flag clears once the car is released back onto the track after
        # having been back in the garage.
        if car.driver_status == DriverStatus.IN_GARAGE:
            self._was_in_garage = True
        elif (
            self._was_in_garage
            and car.driver_status in (DriverStatus.FLYING_LAP, DriverStatus.OUT_LAP)
            and car.pit_status == PitStatus.NONE
        ):
            self.red_flag = False
            self._was_in_garage = False
        summary = self.lap_acc.update(
            current_lap_num=car.current_lap_num,
            last_lap_time_ms=car.last_lap_time_ms,
            sector1_ms=car.sector1_ms,
            sector2_ms=car.sector2_ms,
            pit_status=car.pit_status,
            driver_status=car.driver_status,
            current_lap_invalid=car.current_lap_invalid,
            safety_car_status=self.safety_car_status,
            compound=self.tyre_compound,
            tyre_age_laps=self.tyre_age_laps,
            fuel_remaining_laps=self.fuel_remaining_laps,
        )
        if summary is not None:
            self.laps.append(summary)
        if self._kind() == "qualifying":
            self.run.update(
                t=self._last_session_time or 0.0,
                phase=self._phase(),
                lap_time_ms=car.current_lap_time_ms,
                lap_distance=car.lap_distance,
                sector=car.sector,
                sector1_ms=car.sector1_ms,
                sector2_ms=car.sector2_ms,
                invalid=bool(car.current_lap_invalid),
                ers_pct=self.ers_store_pct,
                lockups=self.lockups.count,
                spins=self.spins.count,
                best_s1_ms=self._player_sectors[0],
                cool_pace_pct=self._th("cool_pace_pct", 10.0),
                decide=self._decide_plan,
                extend_cool=self._cool_extend(),
            )

    def _kind(self) -> str:
        try:
            return SessionType(self.session_type).kind()
        except ValueError:
            return "unknown"

    def _decide_plan(self) -> Plan:
        field_best = tuple(self._best_laps.get(i, 0) for i in range(CAR_SLOTS))
        margin_ms, margin_kind = quali_margin_ms(
            field_best,
            self._player_idx,
            self.num_active_cars,
            self.session_type,
            self._th_map("quali_eliminated", {5: 5, 6: 5, 7: 0}),
        )
        best = self._best_laps.get(self._player_idx, 0)
        lap_s = best / 1000.0 if best > 0 else self._th("release_fallback_lap_s", 95.0)
        return run_plan(
            margin_ms=margin_ms,
            margin_kind=margin_kind,
            safe_margin_ms=int(self._th("quali_safe_margin_ms", 1000.0)),
            ers_pct=self.ers_store_pct,
            ers_min_pct=self._ers_need_pct(),
            hottest_c=max(self._ema_or_zero(self.tyre_inner_fast).as_tuple()),
            tyre_hot_c=self._th("cool_tyre_hot_c", 104.0),
            time_left_s=self.session_time_left,
            cool_lap_s=self._th("cool_lap_factor", 1.3) * lap_s,
            fuel_laps=self.fuel_remaining_laps,
        )

    def _cool_extend(self) -> bool:
        """Cool lap ending with too little battery and time for another cool lap."""
        return (
            self.run.kind == COOL
            and self.ers_store_pct < self._ers_need_pct()
            and self._time_for_cool_and_hot()
        )

    def _ers_need_pct(self) -> float:
        """Battery wanted at the line to push: the configured floor, or what the
        last hot lap spent (all of its start charge if it ran flat)."""
        need = self._th("cool_ers_min_pct", 40.0)
        last = self.run.last_hot
        if last is not None:
            used = last.ers_start_pct - last.ers_end_pct
            if last.ers_end_pct <= 5.0:
                used = max(used, last.ers_start_pct)
            need = max(need, used)
        return min(need, self._th("cool_ers_need_max_pct", 70.0))

    def _time_for_cool_and_hot(self) -> bool:
        best = self._best_laps.get(self._player_idx, 0)
        lap_s = best / 1000.0 if best > 0 else self._th("release_fallback_lap_s", 95.0)
        cool_s = self._th("cool_lap_factor", 1.3) * lap_s
        remaining_s = max(0.0, self.track_length_m - self.lap_distance) / max(
            self.speed_kmh / 3.6, 30.0
        )
        return self.session_time_left > remaining_s + cool_s + lap_s * 0.2

    def _on_event(self, pkt: EventPacket, recv_time: float) -> None:
        if pkt.code == "FLBK":
            self._handle_rewind(pkt.header.session_time)
        elif pkt.code == "RDFL":
            self.red_flag = True
        elif pkt.code == "SSTA":
            self.red_flag = False
        elif pkt.code == "SEND":
            self.session_ended = True
        elif pkt.code == "BUTN" and self._press_bit is not None:
            status = pkt.detail.get("button_status", 0) if isinstance(pkt.detail, dict) else 0
            down = bool(status & self._press_bit)
            if down != self._press_down:
                self._press_down = down
                for cb in self.press_listeners:
                    cb(recv_time, down)

    def _on_session_history(self, pkt: SessionHistoryPacket) -> None:
        self._histories[pkt.car_idx] = pkt
        laps = pkt.laps[: pkt.num_laps]
        best_lap = min(
            (lap for lap in laps if lap.lap_valid_bit_flags & LAP_VALID and lap.lap_time_ms),
            key=lambda lap: lap.lap_time_ms,
            default=None,
        )
        self._best_laps[pkt.car_idx] = best_lap.lap_time_ms if best_lap is not None else 0
        self._best_lap_sectors[pkt.car_idx] = (
            (best_lap.sector1_ms, best_lap.sector2_ms, best_lap.sector3_ms)
            if best_lap is not None
            else (0, 0, 0)
        )
        if pkt.car_idx == self._player_idx:
            self._player_laps_completed = max(0, pkt.num_laps - 1)
            self._player_sectors = (
                min(
                    (
                        lap.sector1_ms
                        for lap in laps
                        if lap.lap_valid_bit_flags & SECTOR1_VALID and lap.sector1_ms
                    ),
                    default=0,
                ),
                min(
                    (
                        lap.sector2_ms
                        for lap in laps
                        if lap.lap_valid_bit_flags & SECTOR2_VALID and lap.sector2_ms
                    ),
                    default=0,
                ),
                min(
                    (
                        lap.sector3_ms
                        for lap in laps
                        if lap.lap_valid_bit_flags & SECTOR3_VALID and lap.sector3_ms
                    ),
                    default=0,
                ),
            )

    def _on_participants(self, pkt: ParticipantsPacket) -> None:
        self.num_active_cars = pkt.num_active_cars
        self.participants = tuple(
            Participant(
                name=c.name,
                team_id=c.team_id,
                ai_controlled=c.ai_controlled,
                race_number=c.race_number,
            )
            for c in pkt.cars
        )

    def _on_car_setups(self, pkt: CarSetupsPacket) -> None:
        car = pkt.cars[self._player_idx]
        self.setup_fuel_load = car.fuel_load
        self.setup_front_wing = car.front_wing
        self.setup_rear_wing = car.rear_wing
        self.setup_brake_bias = car.brake_bias
        self.setup_tyre_pressure = Corners(
            car.rear_left_tyre_pressure,
            car.rear_right_tyre_pressure,
            car.front_left_tyre_pressure,
            car.front_right_tyre_pressure,
        )

    def _on_car_telemetry_2(self, pkt: CarTelemetry2Packet) -> None:
        car = pkt.cars[self._player_idx]
        self.active_aero_mode = car.active_aero_mode
        self.active_aero_available = car.active_aero_available
        self.overtake_available = car.overtake_available
        self.overtake_active = car.overtake_active
        self.overtake_activation_distance_m = car.overtake_activation_distance
        self.driving_wrong_way = bool(car.driving_wrong_way)

    def _on_car_telemetry(self, pkt: CarTelemetryPacket, st: float) -> None:
        self.cars_telemetry = pkt.cars
        car = pkt.cars[self._player_idx]
        self.tyre_surface = car.tyres_surface_temperature
        self.tyre_inner = car.tyres_inner_temperature
        self.brake_temp = car.brakes_temperature
        self.speed_kmh = float(car.speed)
        self.throttle = car.throttle
        self.brake = car.brake
        if car.throttle >= 0.95 and car.brake == 0:
            if self._straight_since is None:
                self._straight_since = st
        else:
            self._straight_since = None
        self.tyre_surface_fast.update(st, car.tyres_surface_temperature)
        self.tyre_surface_slow.update(st, car.tyres_surface_temperature)
        self.tyre_inner_fast.update(st, car.tyres_inner_temperature)
        self.tyre_inner_slow.update(st, car.tyres_inner_temperature)
        self.run_temps.update(
            st, car.tyres_inner_temperature, self._phase() == "flying" and not self.game_paused
        )
        self.brake_fast.update(st, car.brakes_temperature)
        self.brake_slow.update(st, car.brakes_temperature)

    def _on_car_status(self, pkt: CarStatusPacket, st: float) -> None:
        self.cars_status = pkt.cars
        car = pkt.cars[self._player_idx]
        self.front_brake_bias = car.front_brake_bias
        self.boost.update(st, car.ers_deploy_mode)
        self.tyre_compound = car.actual_tyre_compound
        self.tyre_visual = car.visual_tyre_compound
        self.tyre_age_laps = car.tyres_age_laps
        self.fuel_remaining_laps = car.fuel_remaining_laps
        self.fuel_in_tank = car.fuel_in_tank
        cap = 4_000_000.0  # nominal 4 MJ ERS store
        self.ers_store_pct = min(100.0, car.ers_store_energy / cap * 100.0)
        self.ers_deploy_mode = car.ers_deploy_mode
        self.drs_allowed = car.drs_allowed
        self.network_paused = bool(car.network_paused)

    def _on_car_damage(self, pkt: CarDamagePacket) -> None:
        self.cars_damage = pkt.cars
        car = pkt.cars[self._player_idx]
        self.tyres_wear = car.tyres_wear
        self.damage = Damage(
            front_left_wing=car.front_left_wing_damage,
            front_right_wing=car.front_right_wing_damage,
            rear_wing=car.rear_wing_damage,
            floor=car.floor_damage,
            diffuser=car.diffuser_damage,
            sidepod=car.sidepod_damage,
            gearbox=car.gearbox_damage,
            engine=car.engine_damage,
            drs_fault=car.drs_fault,
            ers_fault=car.ers_fault,
        )

    # -- snapshot -----------------------------------------------------------

    def snapshot(self, now: float) -> Snapshot:
        st = self._last_session_time or 0.0
        try:
            kind = SessionType(self.session_type).kind()
        except ValueError:
            kind = "unknown"
        phase = self._phase()
        inner = self._ema_or_zero(self.tyre_inner_fast)
        coldest = min(range(4), key=lambda i: inner.as_tuple()[i])
        lockup, lockup_wheel = self.lockups.recent(st)
        yellow = self.yellows.view(self.lap_distance)
        cars = (
            tuple(
                CarLap(
                    lap_distance=c.lap_distance,
                    current_lap_time_ms=c.current_lap_time_ms,
                    sector=c.sector,
                    sector1_ms=c.sector1_ms,
                    sector2_ms=c.sector2_ms,
                    car_position=c.car_position,
                    driver_status=c.driver_status,
                    pit_status=c.pit_status,
                    result_status=c.result_status,
                )
                for c in self.cars_lap
            )
            if self.cars_lap is not None
            else ()
        )
        field_best = tuple(self._best_laps.get(i, 0) for i in range(CAR_SLOTS))
        player_best_lap = field_best[self._player_idx] if 0 <= self._player_idx < CAR_SLOTS else 0
        player_best_s1, player_best_s2, player_best_s3 = self._player_sectors
        player_laps_completed = self._player_laps_completed
        tyre_sets = tuple(
            TyreSet(
                actual=s.actual_tyre_compound,
                visual=s.visual_tyre_compound,
                wear=s.wear,
                available=s.available,
                life_span=s.life_span,
                usable_life=s.usable_life,
                fitted=s.fitted,
            )
            for s in self._tyre_sets
        )
        fresh = {
            int(VisualCompound.SOFT): 0,
            int(VisualCompound.MEDIUM): 0,
            int(VisualCompound.HARD): 0,
        }
        for s in tyre_sets:
            if s.available and s.wear == 0 and s.visual in fresh:
                fresh[s.visual] += 1
        fitted = (
            tyre_sets[self._tyre_fitted_idx]
            if self._tyre_fitted_idx < len(tyre_sets)
            else TyreSet()
        )
        fresh_current = sum(
            1 for s in tyre_sets if s.available and s.wear == 0 and s.visual == fitted.visual
        )
        on_straight = (
            self._straight_since is not None and st - self._straight_since >= self._straight_hold_s
        )
        release = ReleaseWindow(math.inf, math.inf, False, 0.0, 0)
        cutoff_ms = proj_ms = deficit_ms = 0
        quali_through = abort_advised = False
        abort_reason = ""
        margin_ms, margin_kind = 0, ""
        if kind == "qualifying":
            margin_ms, margin_kind = quali_margin_ms(
                field_best,
                self._player_idx,
                self.num_active_cars,
                self.session_type,
                self._th_map("quali_eliminated", {5: 5, 6: 5, 7: 0}),
            )
            clean_gap = self._th("release_clean_gap_s", 4.0)
            fallback_s = self._th("release_fallback_lap_s", 95.0)
            if phase in ("garage", "pitting") and self.cars_lap is not None:
                player_lap_s = player_best_lap / 1000.0 if player_best_lap > 0 else fallback_s
                out_lap = self._th("release_out_lap_s", 0.0) or 1.3 * player_lap_s
                release = release_window(
                    self.cars_lap,
                    self._player_idx,
                    self.track_length_m,
                    self._th("pit_exit_m", 0.0),
                    out_lap,
                    field_best,
                    fallback_s,
                    clean_gap,
                )
            if phase == "flying":
                cutoff_ms = quali_cutoff_ms(
                    field_best,
                    self.num_active_cars,
                    self.session_type,
                    self._th_map("quali_eliminated", {5: 5, 6: 5, 7: 0}),
                )
                safe_margin = int(self._th("abort_safe_margin_ms", 300.0))
                quali_through = bool(
                    player_best_lap > 0
                    and cutoff_ms > 0
                    and player_best_lap < cutoff_ms - safe_margin
                )
                proj_ms = projected_lap_ms(
                    self.sector,
                    self.current_lap_time_ms,
                    self.sector1_time_ms,
                    self.sector2_time_ms,
                    player_best_s1,
                    player_best_s2,
                    player_best_s3,
                )
                advice = abort_advice(
                    proj_ms,
                    cutoff_ms,
                    player_best_lap,
                    self.sector,
                    fresh_current,
                    self.ers_store_pct,
                    safe_margin_ms=safe_margin,
                    deficit_ms=int(self._th("abort_deficit_ms", 400.0)),
                    ers_keep_pct=self._th("abort_ers_keep_pct", 60.0),
                )
                deficit_ms = advice.deficit_ms
                abort_advised = advice.advised
                abort_reason = advice.reason
        run_avg = self.run_temps.mean()
        pressures = pressure_advice(
            run_avg,
            self.setup_tyre_pressure,
            self._th("pressure_window_low_c", 88.0),
            self._th("pressure_window_high_c", 102.0),
            hot_sign=self._th("pressure_hot_sign", 1.0),
            medium_c=self._th("pressure_size_medium_c", 5.0),
            large_c=self._th("pressure_size_large_c", 10.0),
            steps_psi=(
                self._th("pressure_step_small_psi", 0.2),
                self._th("pressure_step_medium_psi", 0.4),
                self._th("pressure_step_large_psi", 0.8),
            ),
            front_range=self._psi_range("front"),
            rear_range=self._psi_range("rear"),
        )
        cool = self._cool_view(st, kind, phase, player_best_lap, field_best, inner)
        return Snapshot(
            now=now,
            session_time=st,
            last_packet_t=self.last_recv_wall,
            session_kind=kind,
            session_type=self.session_type,
            track_id=self.track_id,
            total_laps=self.total_laps,
            lap_num=self.lap_num,
            lap_distance=self.lap_distance,
            sector=self.sector,
            position=self.position,
            driver_status=self.driver_status,
            pit_status=self.pit_status,
            phase=phase,
            safety_car_status=self.safety_car_status,
            session_uid=self.session_uid or 0,
            session_time_left=self.session_time_left,
            session_duration=self.session_duration,
            track_length_m=self.track_length_m,
            pit_speed_limit=self.pit_speed_limit,
            game_paused=self.game_paused,
            paused=self.game_paused or self.network_paused,
            num_active_cars=self.num_active_cars,
            red_flag=self.red_flag,
            session_ended=self.session_ended,
            rewinds=self.rewinds,
            since_rewind_s=(
                math.inf if self._last_rewind_t is None else max(0.0, st - self._last_rewind_t)
            ),
            cars=cars,
            current_lap_time_ms=self.current_lap_time_ms,
            sector1_time_ms=self.sector1_time_ms,
            sector2_time_ms=self.sector2_time_ms,
            current_lap_invalid=self.current_lap_invalid,
            pit_lane_time_ms=self.pit_lane_time_ms,
            participants=self.participants,
            field_best_laps=tuple(field_best),
            player_best_lap_ms=player_best_lap,
            player_best_s1_ms=player_best_s1,
            player_best_s2_ms=player_best_s2,
            player_best_s3_ms=player_best_s3,
            player_laps_completed=player_laps_completed,
            tyre_sets=tyre_sets,
            fresh_sets_soft=fresh[int(VisualCompound.SOFT)],
            fresh_sets_medium=fresh[int(VisualCompound.MEDIUM)],
            fresh_sets_hard=fresh[int(VisualCompound.HARD)],
            fresh_sets_current=fresh_current,
            fitted_life_span=fitted.life_span,
            setup_fuel_load=self.setup_fuel_load,
            setup_front_wing=self.setup_front_wing,
            setup_rear_wing=self.setup_rear_wing,
            setup_brake_bias=self.setup_brake_bias,
            active_aero_mode=self.active_aero_mode,
            active_aero_available=self.active_aero_available,
            overtake_available=self.overtake_available,
            overtake_active=self.overtake_active,
            overtake_activation_distance_m=self.overtake_activation_distance_m,
            driving_wrong_way=self.driving_wrong_way,
            on_straight=on_straight,
            release_gap_ahead_s=release.gap_ahead_s,
            release_gap_behind_s=release.gap_behind_s,
            release_clean=release.clean,
            release_wait_s=0.0 if math.isinf(release.wait_s) else release.wait_s,
            cars_on_track=release.cars_on_track,
            quali_cutoff_ms=cutoff_ms,
            projected_lap_ms=proj_ms,
            quali_through=quali_through,
            abort_deficit_ms=deficit_ms,
            abort_deficit_s=deficit_ms / 1000.0,
            abort_advised=abort_advised,
            abort_reason=abort_reason,
            tyre_surface=self.tyre_surface,
            tyre_inner=self.tyre_inner,
            brake_temp=self.brake_temp,
            tyre_surface_ema_fast=self._ema_or_zero(self.tyre_surface_fast),
            tyre_surface_ema_slow=self._ema_or_zero(self.tyre_surface_slow),
            tyre_inner_ema_fast=inner,
            tyre_inner_ema_slow=self._ema_or_zero(self.tyre_inner_slow),
            brake_ema_fast=self._ema_or_zero(self.brake_fast),
            brake_ema_slow=self._ema_or_zero(self.brake_slow),
            tyre_compound=self.tyre_compound,
            tyre_visual=self.tyre_visual,
            tyre_age_laps=self.tyre_age_laps,
            fuel_remaining_laps=self.fuel_remaining_laps,
            fuel_in_tank=self.fuel_in_tank,
            ers_store_pct=self.ers_store_pct,
            ers_deploy_mode=self.ers_deploy_mode,
            drs_allowed=self.drs_allowed,
            tyres_wear=self.tyres_wear,
            damage=self.damage,
            speed_kmh=self.speed_kmh,
            throttle=self.throttle,
            brake=self.brake,
            front_brake_bias=self.front_brake_bias,
            tyre_inner_front_c=(inner.fl + inner.fr) / 2,
            tyre_inner_rear_c=(inner.rl + inner.rr) / 2,
            coldest_tyre=WHEEL_NAMES[coldest],
            coldest_tyre_c=inner.as_tuple()[coldest],
            s3_entry_coldest_c=self.s3_entry_coldest_c,
            boost_on_s=self.boost.on_s(st),
            lockup=lockup,
            lockup_wheel=lockup_wheel,
            lockups_this_lap=self.lockups.count_lap,
            lockup_spot_laps=self.lockups.spot_laps,
            spun=self.spins.recent(st),
            spins=self.spins.count,
            yellow_here=yellow.here,
            yellow_ahead_m=_round50(yellow.ahead_m),
            yellow_ahead_sector=yellow.ahead_sector,
            yellow_behind_m=_round50(yellow.behind_m),
            yellow_behind_sector=yellow.behind_sector,
            laps=tuple(self.laps),
            quali_margin_ms=margin_ms,
            quali_margin_s=round(margin_ms / 1000.0, 1),
            quali_margin_kind=margin_kind,
            setup_tyre_pressure=self.setup_tyre_pressure,
            run_tyre_inner_avg=run_avg,
            run_flying_s=self.run_temps.seconds,
            pressure_advice=pressures,
            pressure_advice_text=pressure_text(pressures),
            **cool,
            **self._traffic(player_best_lap),
            dist_to_line_m=(
                max(0.0, self.track_length_m - self.lap_distance)
                if self.track_length_m > 0 and self.lap_distance >= 0
                else math.inf
            ),
            pit_exit_s=(
                st - self._pit_exit_t
                if self._pit_exit_t is not None and st >= self._pit_exit_t
                else math.inf
            ),
            _ages={name: st - t for name, t in self._last_update.items()},
        )

    def _cool_view(
        self,
        st: float,
        kind: str,
        phase: str,
        player_best: int,
        field_best: tuple[int, ...],
        inner: Corners,
    ) -> dict[str, Any]:
        """Run-plan and cool-down-lap fields of the snapshot."""
        out: dict[str, Any] = {}
        if kind != "qualifying":
            return out
        run = self.run
        temps = inner.as_tuple()
        hot_i = max(range(4), key=lambda i: temps[i])
        low_c = self._th("pressure_window_low_c", 88.0)
        high_c = self._th("pressure_window_high_c", 102.0)
        if temps[hot_i] > high_c:
            hint = f"{WHEEL_NAMES[hot_i]} {temps[hot_i]:.0f}, keep it off the kerbs"
        elif min(temps) < low_c:
            cold_i = min(range(4), key=lambda i: temps[i])
            hint = f"{WHEEL_NAMES[cold_i]} down to {temps[cold_i]:.0f}, keep some heat in it"
        else:
            hint = (
                f"tyres in the window, fronts {(inner.fl + inner.fr) / 2:.0f}, "
                f"rears {(inner.rl + inner.rr) / 2:.0f}"
            )
        cool_lap = phase == "flying" and run.kind == COOL
        to_hot = (
            self.track_length_m - self._th("cool_hot_mode_m", 600.0) - self.lap_distance
            if cool_lap and self.track_length_m > 0
            else 0.0
        )
        plan = run.plan
        why = ""
        if plan.reason == "battery":
            why = f"Battery's {self.ers_store_pct:.0f}"
        elif plan.reason == "tyres":
            why = f"{WHEEL_NAMES[hot_i]}'s at {temps[hot_i]:.0f}"
        elif plan.reason == "time":
            why = f"{self.session_time_left / 60:.0f} minutes left"
        elif plan.reason == "fuel":
            why = "Fuel's tight"
        elif plan.reason == "flag":
            why = "That's the flag"
        elif plan.reason == "safe":
            why = "You're safe"
        pole_idx = min(
            (i for i, b in enumerate(field_best) if b > 0), key=lambda i: field_best[i], default=-1
        )
        pole_gap = 0
        gaps = (0, 0, 0)
        name = ""
        if pole_idx >= 0 and pole_idx != self._player_idx and player_best > 0:
            pole_gap = player_best - field_best[pole_idx]
            mine = self._best_lap_sectors.get(self._player_idx, (0, 0, 0))
            theirs = self._best_lap_sectors.get(pole_idx, (0, 0, 0))
            gaps = (
                mine[0] - theirs[0] if mine[0] and theirs[0] else 0,
                mine[1] - theirs[1] if mine[1] and theirs[1] else 0,
                mine[2] - theirs[2] if mine[2] and theirs[2] else 0,
            )
            if pole_idx < len(self.participants):
                name = self.participants[pole_idx].name
        worst = max(range(3), key=lambda i: gaps[i])
        out.update(
            run_lap_kind=run.kind,
            line_crossings=run.crossings,
            run_plan=plan.plan,
            run_plan_reason=plan.reason,
            run_plan_why=why,
            cool_lap=cool_lap,
            cool_prep=cool_lap and self.track_length_m > 0 and to_hot <= 0,
            ers_need_pct=round(self._ers_need_pct()),
            cool_extend=cool_lap and self._cool_extend(),
            time_for_cool_and_hot=self._time_for_cool_and_hot(),
            cool_elapsed_s=max(0.0, st - run.cool_start_t) if cool_lap else 0.0,
            dist_to_hot_mode_m=max(0.0, to_hot),
            last_hot=run.last_hot,
            last_hot_mistakes=(
                mistakes_text(run.last_hot, self._player_sectors) if run.last_hot else ""
            ),
            hottest_tyre=WHEEL_NAMES[hot_i],
            hottest_tyre_c=temps[hot_i],
            cool_tyre_hint=hint,
            pole_driver=name,
            pole_gap_ms=pole_gap,
            pole_gap_s=round(pole_gap / 1000.0, 1),
            pole_sector_gaps_ms=gaps,
            pole_worst_sector=worst + 1 if gaps[worst] > 0 else 0,
            pole_worst_sector_s=round(max(0, gaps[worst]) / 1000.0, 1),
            hot_car_behind_s=(
                self._hot_car_behind_s() if cool_lap or phase == "out_lap" else math.inf
            ),
        )
        return out

    def _psi_range(self, axle: str) -> tuple[float, float] | None:
        lo = self._th(f"pressure_{axle}_min_psi", 0.0)
        hi = self._th(f"pressure_{axle}_max_psi", 0.0)
        return (lo, hi) if 0 < lo < hi else None

    def _traffic(self, player_best_ms: int) -> dict[str, Any]:
        """Nearest car on track ahead and behind the player, within 1500 m."""
        out: dict[str, Any] = {}
        if self.cars_lap is None or self.track_length_m <= 0 or self.pit_status != PitStatus.NONE:
            return out
        length = self.track_length_m
        push_ms = length / (player_best_ms / 1000.0) if player_best_ms > 0 else 55.0
        ahead: tuple[float, int] | None = None
        behind: tuple[float, int] | None = None
        for i, c in enumerate(self.cars_lap):
            if i == self._player_idx or c.pit_status != PitStatus.NONE:
                continue
            if c.driver_status == DriverStatus.IN_GARAGE or c.result_status != 2:
                continue
            fwd = (c.lap_distance - self.lap_distance) % length
            back = length - fwd
            if 0 < fwd <= 1500 and (ahead is None or fwd < ahead[0]):
                ahead = (fwd, i)
            if 0 < back <= 1500 and (behind is None or back < behind[0]):
                behind = (back, i)
        if ahead is not None:
            mine = self.speed_kmh / 3.6
            theirs = mine
            if self.cars_telemetry is not None and ahead[1] < len(self.cars_telemetry):
                theirs = float(self.cars_telemetry[ahead[1]].speed) / 3.6
            closing = mine - theirs
            out.update(
                traffic_ahead_m=round(ahead[0]),
                traffic_ahead_s=round(ahead[0] / push_ms, 1),
                traffic_ahead_closing_s=(
                    round(ahead[0] / closing, 1) if closing > 1.0 else math.inf
                ),
                traffic_ahead_slow=closing * 3.6 >= self._th("slow_car_delta_kmh", 60.0),
                traffic_ahead_kind=_lap_kind(self.cars_lap[ahead[1]].driver_status),
            )
        if behind is not None:
            speed = 0.0
            if self.cars_telemetry is not None and behind[1] < len(self.cars_telemetry):
                speed = float(self.cars_telemetry[behind[1]].speed) / 3.6
            out.update(
                traffic_behind_m=round(behind[0]),
                traffic_behind_s=round(behind[0] / max(speed, 30.0), 1),
                traffic_behind_kind=_lap_kind(self.cars_lap[behind[1]].driver_status),
            )
        return out

    def _hot_car_behind_s(self) -> float:
        """Seconds until the nearest car on a flying lap behind reaches the player."""
        if self.cars_lap is None or self.track_length_m <= 0:
            return math.inf
        best = math.inf
        for i, c in enumerate(self.cars_lap):
            if i == self._player_idx or c.driver_status != DriverStatus.FLYING_LAP:
                continue
            if c.pit_status != PitStatus.NONE:
                continue
            gap_m = (self.lap_distance - c.lap_distance) % self.track_length_m
            if gap_m <= 0 or gap_m > 1500:
                continue
            speed = 0.0
            if self.cars_telemetry is not None and i < len(self.cars_telemetry):
                speed = float(self.cars_telemetry[i].speed) / 3.6
            best = min(best, gap_m / max(speed, 30.0))
        return best

    def _th(self, name: str, default: float) -> float:
        try:
            return float(self._thresholds.get(name, default))
        except (TypeError, ValueError):
            return default

    def _th_map(self, name: str, default: Mapping[int, int]) -> Mapping[int, int]:
        v = self._thresholds.get(name)
        if isinstance(v, Mapping):
            return v
        return default

    def _phase(self) -> str:
        if self.red_flag:
            return "red_flag"
        if self.driver_status == DriverStatus.IN_GARAGE:
            return "garage"
        if self.pit_status in (PitStatus.PITTING, PitStatus.IN_PIT_AREA):
            return "pitting"
        try:
            return _PHASE_BY_DRIVER_STATUS[DriverStatus(self.driver_status)]
        except (ValueError, KeyError):
            return "on_track"

    @staticmethod
    def _ema_or_zero(ema: CornersEma) -> Corners:
        vals = [e.value if e.value is not None else 0.0 for e in ema.emas.as_tuple()]
        return Corners(*vals)
