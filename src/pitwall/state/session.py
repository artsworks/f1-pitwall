"""SessionState: single mutable model updated per packet; Snapshot is the
frozen view rules read. Player car only for M1 (24-slot arrays kept)."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from pitwall.model.deg import rival_pace_ms
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
    WeatherForecastSample,
    Zone,
    parse,
)
from pitwall.state.driving import (
    WHEEL_NAMES,
    BoostTimer,
    ContactTracker,
    LockupDetector,
    OffTrackTracker,
    SaveDetector,
    SpinDetector,
    YellowTracker,
)
from pitwall.state.ema import CornersEma
from pitwall.state.lap import LapAccumulator, LapSummary
from pitwall.state.model_view import ModelView
from pitwall.state.pressure import PressureCall, RunTemps, pressure_advice, pressure_text
from pitwall.state.quali import (
    ReleaseWindow,
    abort_advice,
    projected_lap_ms,
    quali_cutoff_ms,
    quali_margin_ms,
    release_window,
)
from pitwall.state.race import RacePhase, penalty_standing, relevant_rivals
from pitwall.state.runplan import COOL, HotLap, Plan, RunTracker, mistakes_text, run_plan
from pitwall.strategy.plans import StrategyPlan


def thermal_window(thresholds: Mapping[str, Any], compound: int) -> tuple[float, float]:
    cold_map = thresholds.get("tyre_inner_cold_by_compound_c", {})
    hot_map = thresholds.get("tyre_inner_hot_by_compound_c", {})
    cold = cold_map.get(compound, thresholds.get("tyre_inner_cold_c", 80.0))
    hot = hot_map.get(compound, thresholds.get("tyre_inner_hot_c", 110.0))
    return float(cold), float(hot)


def pressure_window(thresholds: Mapping[str, Any], compound: int) -> tuple[float, float]:
    cold_map = thresholds.get("tyre_inner_cold_by_compound_c", {})
    if compound not in cold_map:
        return (
            float(thresholds.get("pressure_window_low_c", 88.0)),
            float(thresholds.get("pressure_window_high_c", 102.0)),
        )
    inset = float(thresholds.get("pressure_window_inset_c", 5.0))
    cold, hot = thermal_window(thresholds, compound)
    return cold + inset, hot - inset


def spoken_lap_time(ms: float) -> str:
    if ms <= 0 or not math.isfinite(ms):
        return "?"
    minutes, seconds = divmod(round(ms / 100) / 10, 60)
    seconds_text = f"{seconds:.1f}".removesuffix(".0")
    if minutes == 0:
        return f"{seconds_text} seconds"
    unit = "minute" if minutes == 1 else "minutes"
    return f"{int(minutes)} {unit} {seconds_text} seconds"


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


# PENA penalty_type -> announced kind. Other types (warning 5, lap invalidated
# 10-15, retired 16, black-flag timer 17, ...) are not penalties to announce.
_PENALTY_KINDS = {0: "drive_through", 1: "stop_go", 4: "time"}
PENALTY_TYPE_WARNING = 5


def _warning_family(kind: str) -> str:
    """Warnings the game counts together toward a penalty: corner cuts, running wide."""
    return kind if kind in ("cut", "overtake") else "limits"


# PENA infringement_type -> kind of track-limit / corner-cutting warning.
_TRACK_WARNING_KINDS = {
    7: "cut",
    8: "overtake",
    9: "overtake",
    27: "minor",
    28: "significant",
    29: "extreme",
}


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
_CORNER_WORDS = ("rear left", "rear right", "front left", "front right")  # wire order


@dataclass(frozen=True, slots=True)
class CarLap:
    """Per-car lap data for one tick (all 24 cars)."""

    lap_distance: float = 0.0
    total_distance: float = 0.0
    current_lap_time_ms: int = 0
    last_lap_time_ms: int = 0
    sector: int = 0
    sector1_ms: int = 0
    sector2_ms: int = 0
    car_position: int = 0
    driver_status: int = 0
    pit_status: int = 0
    result_status: int = 0
    current_lap_num: int = 0
    delta_to_car_in_front_ms: int = 0
    delta_to_race_leader_ms: int = 0
    num_pit_stops: int = 0
    penalties: int = 0
    total_warnings: int = 0
    corner_cutting_warnings: int = 0
    num_unserved_drive_through_pens: int = 0
    num_unserved_stop_go_pens: int = 0


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
    front_wing_pit_status: str = ""
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
    saved: bool = False  # for a few seconds after a slide the driver caught
    saves: int = 0  # this session
    save_peak_deg: float = 0.0  # sideslip at the worst of the last save
    off_track_lost: int = 0  # places lost to the last trip off track, for a few seconds
    off_track_recovered: bool = False  # those places came back on the same lap
    is_sprint: bool = False  # race session with another race later in the weekend
    yellow_here: bool = False
    yellow_ahead_m: float = math.inf
    yellow_ahead_sector: int = 0
    yellow_behind_m: float = math.inf
    yellow_behind_sector: int = 0
    laps: tuple[LapSummary, ...] = ()
    # M2: session context
    session_uid: int = 0
    weekend_link: int = 0
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
    weather: int = 0
    game_mode: int = 0
    since_rewind_s: float = math.inf
    # M3 race engine (docs/18). Model fields mirror ModelView; set_model()
    # fills them.
    laps_remaining: int = 0
    race_phase: str = ""
    sc_laps: int = 0
    lights_out: bool = False
    chequered: bool = False
    fastest_lap_ms: int = 0  # race fastest lap (FTLP event)
    fastest_lap_mine: bool = False
    fastest_lap_name: str = ""
    fastest_lap_time: str = ""  # "1:19.195"
    fastest_lap_spoken: str = ""
    fastest_lap_age_s: float = math.inf  # since it was set
    fastest_lap_gap_s: float = math.inf  # player best minus fastest lap
    grid_position: int = 0
    positions_gained: int = 0  # grid slot minus current position (+ = gained)
    sc_ending: bool = False  # SC in this lap / VSC ending (SCAR event)
    neutral_ended_s: float = math.inf  # since the last SC/VSC ended
    neutral_ended_kind: str = ""  # 'sc' | 'vsc'
    puncture_corner: str = ""  # e.g. "rear left"; tyre damage far above wear
    # Contact (COLL events): 'checking' right after a hit, then 'report'
    contact_phase: str = ""
    contact_name: str = ""
    contact_teammate: bool = False
    contact_hits: int = 0  # hits in this episode
    contact_episodes: int = 0  # contact episodes this session
    contact_damage: str = ""  # part with the biggest new damage, e.g. "front left wing"
    contact_damage_pct: int = 0
    contact_damage_major: bool = False
    teammate_name: str = ""
    teammate_fight: bool = False  # teammate directly ahead/behind within fight range
    teammate_gap_s: float = math.inf
    gap_ahead_s: float = math.inf
    gap_behind_s: float = math.inf
    rival_ahead_idx: int = -1
    rival_behind_idx: int = -1
    rival_pit_exit_idx: int = -1
    rival_ahead_pace_ms: int = 0
    rival_behind_pace_ms: int = 0
    rival_pit_exit_pace_ms: int = 0
    rival_ahead_name: str = ""
    rival_behind_name: str = ""
    rival_pit_exit_name: str = ""
    rival_ahead_age: int = 0
    rival_behind_age: int = 0
    rival_ahead_pitted: bool = False
    rival_ahead_in_pit_lane: bool = False
    rival_behind_pitted: bool = False
    rival_ahead_pos: int = 0
    rival_behind_pos: int = 0
    rival_ahead_compound: int = 0
    rival_behind_compound: int = 0
    # Gap change per lap (s, + = gap shrinking), measured line to line.
    gap_trend_ahead_s: float = 0.0
    gap_trend_behind_s: float = 0.0
    rival_data_restricted: bool = False
    pit_exit_rival_gap_s: float = math.inf
    pit_exit_clean: bool = False
    deg_fit_source: str = ""
    deg_ms_per_lap: float = 0.0
    deg_confidence: float = 0.0
    base_pace_ms: float = 0.0
    laps_of_pace: float = math.inf
    wear_mean_pct: float = 0.0
    wear_max_pct: float = 0.0
    # Field lap-time gap between compound groups (s, + = the drier tyre is
    # quicker): median last lap of inter runners minus slick runners, and of
    # wet runners minus inter runners. 0 when a group has too few cars.
    slick_gain_s: float = 0.0
    inter_gain_s: float = 0.0
    tyre_switch_to: str = ""  # 'slicks' | 'inters' | 'wets' when the field says switch
    wear_per_lap_pct: float = 0.0
    blister_max_pct: int = 0
    wear_hot_corner: str = ""
    wear_hot_ratio: float = 0.0
    wear_hot_rate: float = 0.0
    wear_hot_pct: float = 0.0
    graining: bool = False
    overheat: bool = False
    pit_loss_s: float = 0.0
    pit_loss_source: str = ""
    fuel_margin_laps: float = 0.0
    fuel_per_lap_kg: float = 0.0
    fuel_source: str = ""
    energy_per_lap_mj: float = 0.0
    energy_lap_delta_mj: float = 0.0
    energy_laps_to_floor: float = math.inf
    energy_mode: str = ""
    drs_zone_ahead: bool = False
    drs_available: bool = False
    penalty_s: int = 0
    penalty_type: int = 0
    penalty_infringement: int = 0
    penalty_time_s: int = 0
    penalty_kind: str = ""  # 'time' | 'drive_through' | 'stop_go' for the latest real penalty
    unserved_drive_through: int = 0
    unserved_stop_go: int = 0
    warnings: int = 0
    corner_cut_warnings: int = 0
    penalty_recent: bool = False
    penalty_position: int = 0  # race position once every car's time penalties apply
    penalty_margin_s: float = (
        math.inf
    )  # over the closest car behind after penalties (<0 = he's ahead)
    penalty_threat_name: str = ""
    track_warning_kind: str = ""  # latest track-limit warning: 'minor' | 'significant' | ...
    track_warning_recent: bool = False
    track_warning_count: int = 0  # warnings of the latest warning's kind (cut / track limits)
    blue_flag: bool = False
    weather_now: int = 0
    rain_pct_now: int = 0
    rain_pct_in_10: int = 0
    rain_pct_in_30: int = 0
    weather_in_10: int = -1  # forecast weather type (0 clear .. 5 storm), -1 unknown
    weather_in_30: int = -1
    weather_crossover: str = ""
    weather_crossover_pct: int = 0  # forecast rain chance driving the crossover
    weather_crossover_min: int = 0  # ... and how many minutes out
    pit_plan: str = ""
    pit_plan_lap: int = 0
    # Driver menu opinion (docs/12): "understeer" | "oversteer" | "" while it holds.
    driver_balance: str = ""
    pit_plan_gain_s: float = 0.0
    pit_plan_confidence: float = 0.0
    pit_plan_risk: float = 0.0
    pit_plan_rival_idx: int = -1
    pit_plan_rival_name: str = ""
    pit_plan_reason: str = ""
    pit_window_start: int = 0
    pit_window_end: int = 0
    undercut_s: float = 0.0
    overcut_s: float = 0.0
    plans: tuple[StrategyPlan, ...] = ()
    active_plan: str = ""
    on_plan: bool = True
    plan_label: str = ""
    plan_spoken: str = ""
    plan_stops_left: int = 0
    plan_target_lap: int = 0
    plan_window_start: int = 0
    plan_window_end: int = 0
    plan_window_text: str = ""
    plan_window_open: bool = False
    plan_next_compound: str = ""
    plan_off_s: float = 0.0
    plan_switch_count: int = 0
    plan_switched_from: str = ""
    plan_switch_reason: str = ""
    plan_switch_lap: int = 0
    plan_target_shift: int = 0
    plan_b_spoken: str = ""
    plan_b_delta_s: float = 0.0
    plan_c_spoken: str = ""
    plan_c_delta_s: float = 0.0
    # Battle state (docs/20 L3, pitwall.strategy.battle), filled by the engine.
    battle_mode: str = "free_air"
    battle_mode_laps: int = 0
    battle_catch_laps: float = math.inf
    battle_threat_laps: float = math.inf
    battle_closing_ahead_s: float = 0.0
    battle_closing_behind_s: float = 0.0
    battle_tyre_offset_ahead: int = 0
    battle_tyre_offset_behind: int = 0
    battle_pass_prob: float = 0.0
    battle_hold_prob: float = 0.0
    battle_result: str = ""
    battle_result_recent: bool = False
    battle_result_name: str = ""
    battle_pace_ahead: str = ""  # "three tenths slower": the car ahead vs us, per lap
    battle_pace_behind: str = ""
    player_last_lap_ms: int = 0
    predicted_lap_ms: int = 0
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
    setup_on_throttle_diff: int = 0
    setup_off_throttle_diff: int = 0
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
    setup: Mapping[str, float] = field(default_factory=dict)
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
    next_lap_invalid: bool = False  # track limits deleted this lap before it started
    cool_elapsed_s: float = 0.0
    ers_need_pct: float = 0.0  # battery wanted at the line before pushing
    cool_extend: bool = False  # cool lap ending short of battery, time for another
    time_for_cool_and_hot: bool = False  # finish this lap slow, then one more hot lap
    time_for_out_lap: bool = True  # leaving the garage now still starts a hot lap
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
    best_sectors_ms: tuple[int, int, int] = (0, 0, 0)
    pole_sectors_ms: tuple[int, int, int] = (0, 0, 0)
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

    @property
    def fuel_margin_r(self) -> float:
        return round(self.fuel_margin_laps, 1) + 0.0

    @property
    def positions_lost(self) -> int:
        return max(0, -self.positions_gained)

    @property
    def penalty_places(self) -> int:
        return max(0, self.penalty_position - self.position) if self.penalty_position else 0

    @property
    def classified_position(self) -> int:
        return self.penalty_position if self.penalty_position > 0 else self.position

    @property
    def classified_gained(self) -> int:
        return (
            self.grid_position - self.classified_position
            if self.grid_position and self.classified_position
            else 0
        )

    @property
    def classified_lost(self) -> int:
        return max(0, -self.classified_gained)

    @property
    def penalty_need_s(self) -> float:
        return max(0.0, -self.penalty_margin_s) if math.isfinite(self.penalty_margin_s) else 0.0

    @property
    def fuel_short_laps(self) -> float:
        return max(0.0, -self.fuel_margin_laps)

    @property
    def energy_under_mj(self) -> float:
        return max(0.0, -self.energy_lap_delta_mj)

    @property
    def penalty_kind_text(self) -> str:
        return {"drive_through": "drive-through", "stop_go": "stop-go"}.get(
            self.penalty_kind, self.penalty_kind
        )


class SessionState:
    """Mutable session model. Handlers are registered on Ingest per packet id."""

    def __init__(
        self,
        ema_fast_s: float = 3.0,
        ema_slow_s: float = 30.0,
        *,
        straight_hold_s: float = 1.0,
        press_bit: int | None = None,
        toggle_bit: int | None = None,
        action_bits: Mapping[str, int] | None = None,
        thresholds: Mapping[str, Any] | None = None,
    ) -> None:
        self.last_packet_t: float | None = None
        self.last_recv_wall: float | None = None
        self._player_idx = 0
        self.session_uid: int | None = None
        self.weekend_link_identifier = 0
        # Called with session_time on each flashback rewind.
        self.rewind_listeners: list[Callable[[float], None]] = []
        # Called with the new session_uid on each session change.
        self.session_listeners: list[Callable[[int], None]] = []
        self.session_end_listeners: list[Callable[[], None]] = []
        # Called with (recv_time, down) on each UDP-action button edge.
        self.press_listeners: list[Callable[[float, bool], None]] = []
        # Called with recv_time on each press of the radio-silent toggle button.
        self.toggle_listeners: list[Callable[[float], None]] = []
        # Called with (kind, recv_time) on each press of a named action button
        # (e.g. "mindset" on UDP Action 2, "page" on UDP Action 4).
        self.action_listeners: list[Callable[[str, float], None]] = []
        self._action_bits = {k: v for k, v in (action_bits or {}).items() if v}
        self._action_down: dict[str, bool] = {}
        self._ema_fast_s = ema_fast_s
        self._ema_slow_s = ema_slow_s
        self._straight_hold_s = straight_hold_s
        self._press_bit = press_bit
        self._toggle_bit = toggle_bit or None
        self._toggle_down = False
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
        self.weekend_link_identifier = 0
        self.total_laps = 0
        self.weekend_structure: tuple[int, ...] = ()
        self.safety_car_status = 0
        self.session_time_left = 0.0
        self.session_duration = 0.0
        self.track_length_m = 0.0
        self.pit_speed_limit = 0
        self.game_paused = False
        self.network_paused = False
        self.weather = 0
        self.game_mode = 0
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
        self.setup: dict[str, float] = {}
        self._pressure_base: Corners | None = None
        self.tyre_compound = 0
        self.tyre_visual = 0
        self.tyre_age_laps = 0
        self.fuel_remaining_laps = 0.0
        self.fuel_in_tank = 0.0
        self.blister_max_pct = 0
        self.ers_store_pct = 0.0
        self.ers_store_energy_j = 0.0
        self.ers_deployed_this_lap_j = 0.0
        self.ers_harvested_mguk_j = 0.0
        self.ers_harvested_mguh_j = 0.0
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
        self.contacts = ContactTracker()
        self.saves = SaveDetector()
        self.off_track = OffTrackTracker()
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
        # Newly completed rival laps from Session History (car_idx, lap);
        # drained by Engine._write_laps into SQLite.
        self.rival_laps: list[tuple[int, LapSummary]] = []
        self._rival_laps_emitted: dict[int, int] = {}

        self.cars_lap: tuple[Any, ...] | None = None
        self.cars_telemetry: tuple[Any, ...] | None = None
        self.cars_status: tuple[Any, ...] | None = None
        # (rival idx, gap s) now / at the last two line crossings, for gap trends.
        self._gap_now: dict[str, tuple[int, float]] = {}
        self._gap_lines: list[dict[str, tuple[int, float]]] = []
        self._ahead_latch: tuple[int, float] = (-1, math.inf)
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
        self.setup_on_throttle_diff = 0
        self.setup_off_throttle_diff = 0
        self.active_aero_mode = 0
        self.active_aero_available = 0
        self.overtake_available = 0
        self.overtake_active = 0
        self.overtake_activation_distance_m = 0
        self.driving_wrong_way = False

        # M3 race state (docs/18)
        self.result_status = 0
        self.delta_to_car_in_front_ms = 0
        self.num_pit_stops = 0
        self.penalty_s = 0
        self.warnings = 0
        self.corner_cut_warnings = 0
        self.unserved_drive_through = 0
        self.unserved_stop_go = 0
        self.vehicle_fia_flags = 0
        self.penalty_type = 0
        self.penalty_kind = ""
        self.penalty_infringement = 0
        self.penalty_time_s = 0
        self._last_penalty_st: float | None = None
        self.track_warning_kind = ""
        self._last_track_warning_st: float | None = None
        self._track_warnings: dict[str, int] = {}
        self._warning_events: list[tuple[float, str]] = []
        self._penalty_pending_s = 0
        self._penalty_lap_increase_s = 0
        self._penalty_lap_change_st: float | None = None
        self.lights_out = False
        self.chequered = False
        self._flag_as_leader = False
        self._fastest_lap: tuple[int, int, float] | None = None  # (car idx, ms, session time)
        self.grid_position = 0
        self.sc_ending = False
        self.puncture_corner = ""
        self._wear_marks: list[tuple[float, ...]] = []
        self.wear_hot_corner = ""
        self.wear_hot_ratio = 0.0
        self.wear_hot_rate = 0.0
        self.wear_hot_pct = 0.0
        self._race = RacePhase()
        self.race_phase = "formation"
        self.sc_laps = 0
        self._prev_lap_num = 0
        self._cars_pitted_this_lap: set[int] = set()
        self._pitted_lap_snapshot: set[int] = set()
        self._restricted_streak = 0
        self.rival_data_restricted = False
        self._forecast_samples: tuple[WeatherForecastSample, ...] = ()
        self._drs_zones: tuple[Zone, ...] = ()
        self._aero_zones: tuple[Zone, ...] = ()
        self._weather_crossover = ""
        self._weather_crossover_pct = 0
        self._weather_crossover_min = 0
        self._overheat = False
        self._graining = False
        self._model = ModelView()
        self._thermal_warn_offset_c = 0.0

    @property
    def model(self) -> ModelView:
        return self._model

    def set_model(self, view: ModelView) -> None:
        """Model outputs for the next snapshot (Engine -> state -> snapshot)."""
        self._model = view

    def set_mode_offsets(self, *, thermal_warn_offset_c: float) -> None:
        """Mindset-dependent warning offsets (resolved in the Engine)."""
        self._thermal_warn_offset_c = thermal_warn_offset_c

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
            for end_listener in self.session_end_listeners:
                end_listener()
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
            self.saves.update(st, pkt.local_velocity)

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
        self.saves.reset()
        self.contacts.reset()
        self.off_track.reset()
        self.boost.reset()
        self._warning_events = [(when, kind) for when, kind in self._warning_events if when <= t]
        self._track_warnings.clear()
        for _, kind in self._warning_events:
            family = _warning_family(kind)
            self._track_warnings[family] = self._track_warnings.get(family, 0) + 1
        self.track_warning_kind = self._warning_events[-1][1] if self._warning_events else ""
        self._last_track_warning_st = self._warning_events[-1][0] if self._warning_events else None
        self._penalty_pending_s = 0
        self._penalty_lap_increase_s = 0
        self._penalty_lap_change_st = None
        self._last_penalty_st = None
        self.lap_acc.note_flashback()
        self.run.note_rewind()
        # Per-car session history stays: it is authoritative from the game and
        # is refreshed per car after a rewind. LapData-derived caches reset.
        self.cars_lap = None
        self._gap_now = {}
        self._gap_lines = []
        self._ahead_latch = (-1, math.inf)
        self._straight_since = None
        for cb in self.rewind_listeners:
            cb(t)

    # -- per-packet updates -------------------------------------------------

    def _on_session(self, pkt: SessionPacket) -> None:
        self.session_type = pkt.session_type
        self.track_id = pkt.track_id
        self.total_laps = pkt.total_laps
        self.weekend_structure = tuple(pkt.weekend_structure[: pkt.num_sessions_in_weekend])
        self.weekend_link_identifier = pkt.weekend_link_identifier
        self.safety_car_status = pkt.safety_car_status
        self.session_time_left = float(pkt.session_time_left)
        self.session_duration = float(pkt.session_duration)
        self.track_length_m = float(pkt.track_length)
        self.pit_speed_limit = pkt.pit_speed_limit
        self.game_paused = bool(pkt.game_paused)
        self.weather = pkt.weather
        self.game_mode = pkt.game_mode
        self._forecast_samples = pkt.weather_forecast_samples[: pkt.num_weather_forecast_samples]
        self._drs_zones = pkt.drs_zones[: pkt.num_drs_zones]
        self._aero_zones = (
            pkt.active_aero_zones_full[: pkt.num_active_aero_zones_full]
            + pkt.active_aero_zones_partial[: pkt.num_active_aero_zones_partial]
        )
        self._update_weather_crossover()
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
        lap_boundary = car.current_lap_num != self.lap_num and self.lap_num != 0
        if lap_boundary:
            self._pitted_lap_snapshot = self._cars_pitted_this_lap
            self._cars_pitted_this_lap = set()
            self._note_lap_boundary()
            self._note_corner_wear()
        for i, c in enumerate(pkt.cars):
            if i != self._player_idx and c.pit_status != 0:
                self._cars_pitted_this_lap.add(i)
        length = self.track_length_m
        line_after_flag = self.chequered and (
            self._flag_as_leader
            or (
                length > 0
                and self.lap_distance > 0.8 * length
                and 0 <= car.lap_distance < 0.2 * length
            )
        )
        self.lap_num = car.current_lap_num
        self.lap_distance = car.lap_distance
        if car.sector == 2 and self.sector != 2:
            self.s3_entry_coldest_c = min(self._ema_or_zero(self.tyre_inner_fast).as_tuple())
        self.sector = car.sector
        self.position = car.car_position
        self.off_track.update_position(
            car.car_position, car.current_lap_num, car.pit_status != PitStatus.NONE
        )
        if car.driver_status == DriverStatus.OUT_LAP and self.driver_status != DriverStatus.OUT_LAP:
            self.run_temps.reset()
            self._pressure_base = None
        self.driver_status = car.driver_status
        if self.pit_status != PitStatus.NONE and car.pit_status == PitStatus.NONE:
            self._pit_exit_t = self._last_session_time
        self.pit_status = car.pit_status
        self.current_lap_time_ms = car.current_lap_time_ms
        self.sector1_time_ms = car.sector1_ms
        self.sector2_time_ms = car.sector2_ms
        self.current_lap_invalid = car.current_lap_invalid
        self.pit_lane_time_ms = car.pit_lane_time_in_lane_ms
        self.result_status = car.result_status
        self.delta_to_car_in_front_ms = car.delta_to_car_in_front_ms
        self.num_pit_stops = car.num_pit_stops
        if car.penalties != self.penalty_s:
            self._penalty_lap_increase_s = max(0, car.penalties - self.penalty_s)
            self._penalty_lap_change_st = pkt.header.session_time
        self.penalty_s = car.penalties
        if car.penalties >= self._penalty_pending_s:
            self._penalty_pending_s = 0
        self.warnings = car.total_warnings
        self.corner_cut_warnings = car.corner_cutting_warnings
        self.unserved_drive_through = car.num_unserved_drive_through_pens
        self.unserved_stop_go = car.num_unserved_stop_go_pens
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
        st = self._last_session_time or 0.0
        self.race_phase = self._race.update(
            session_time=st,
            safety_car_status=self.safety_car_status,
            pit_status=car.pit_status,
            driver_status=car.driver_status,
            result_status=car.result_status,
            lap_num=car.current_lap_num,
            lap_boundary=lap_boundary or line_after_flag,
            lights_out_seen=self.lights_out,
            chequered_seen=self.chequered,
            red_flag=self.red_flag,
            sc_exit_hold_s=self._th("sc_exit_hold_s", 5.0),
        )
        self.sc_laps = self._race.sc_laps
        if self.race_phase not in ("sc", "vsc"):
            self.sc_ending = False
        if car.grid_position and not self.grid_position:
            self.grid_position = car.grid_position
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
            wear_mean_pct=sum(self.tyres_wear.as_tuple()) / 4.0,
            fuel_in_tank=self.fuel_in_tank,
            ers_deployed_this_lap=self.ers_deployed_this_lap_j,
            weather=self.weather,
            visual=self.tyre_visual,
        )
        if summary is not None:
            self.laps.append(summary)
            self._gap_lines = [*self._gap_lines[-1:], dict(self._gap_now)]
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

    def _is_sprint(self) -> bool:
        if self._kind() != "race" or self.session_type not in self.weekend_structure:
            return False
        later = self.weekend_structure[self.weekend_structure.index(self.session_type) + 1 :]
        return any(15 <= t <= 17 for t in later)

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
            tyre_hot_c=(
                thermal_window(self._thresholds, self.tyre_compound)[1]
                - self._th("cool_tyre_hot_margin_c", 1.0)
                if self.tyre_compound in self._thresholds.get("tyre_inner_hot_by_compound_c", {})
                else self._th("cool_tyre_hot_c", 104.0)
            ),
            time_left_s=self.session_time_left,
            cool_lap_s=self._th("cool_lap_factor", 1.3) * lap_s,
            fuel_laps=self.fuel_remaining_laps,
            fuel_push_laps=self._th("fuel_push_need_laps", 2.0),
            fuel_cool_laps=self._th("fuel_cool_need_laps", 3.0),
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

    def _time_for_out_lap(self) -> bool:
        """Garage to the line before the flag: out lap plus pit-lane allowance."""
        best = self._best_laps.get(self._player_idx, 0)
        lap_s = best / 1000.0 if best > 0 else self._th("release_fallback_lap_s", 95.0)
        need = self._th("out_lap_factor", 1.3) * lap_s + self._th("out_lap_pit_s", 40.0)
        return self.session_time_left > need

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
            self.lap_acc.note_red_flag()
        elif pkt.code == "SSTA":
            self.red_flag = False
        elif pkt.code == "SEND":
            self.session_ended = True
        elif pkt.code == "LGOT":
            # A red-flag restart is a new standing start: lights out ends the red flag
            # and any safety car left over from before it.
            self.lights_out = True
            self.red_flag = False
            self.safety_car_status = 0
        elif pkt.code == "CHQF":
            self.chequered = True
            self._flag_as_leader = self.position == 1
        elif pkt.code == "COLL":
            if isinstance(pkt.detail, dict):
                a = int(pkt.detail.get("vehicle1_idx", 255))
                b = int(pkt.detail.get("vehicle2_idx", 255))
                if self._player_idx in (a, b):
                    self.contacts.merge_s = self._th("contact_merge_s", 8.0)
                    self.contacts.hit(
                        pkt.header.session_time,
                        b if a == self._player_idx else a,
                        int(pkt.detail.get("severity", 0)),
                        self._damage_parts(),
                    )
        elif pkt.code == "FTLP":
            if isinstance(pkt.detail, dict):
                ms = round(float(pkt.detail.get("lap_time_s", 0.0)) * 1000)
                if ms > 0:
                    idx = int(pkt.detail.get("vehicle_idx", 255))
                    self._fastest_lap = (idx, ms, pkt.header.session_time)
        elif pkt.code == "SCAR":
            if isinstance(pkt.detail, dict):
                # event_type: 0 deployed, 1 returning (SC in / VSC ending), 2 returned, 3 resume
                self.sc_ending = int(pkt.detail.get("event_type", 0)) in (1, 2)
        elif pkt.code == "PENA":
            if isinstance(pkt.detail, dict) and pkt.detail.get("vehicle_idx") == self._player_idx:
                ptype = int(pkt.detail.get("penalty_type", 0))
                kind = _PENALTY_KINDS.get(ptype, "")
                infringement = int(pkt.detail.get("infringement_type", 0))
                warn_kind = _TRACK_WARNING_KINDS.get(infringement, "")
                if ptype == PENALTY_TYPE_WARNING and warn_kind:
                    self.track_warning_kind = warn_kind
                    self._last_track_warning_st = pkt.header.session_time
                    self._warning_events.append((pkt.header.session_time, warn_kind))
                    family = _warning_family(warn_kind)
                    self._track_warnings[family] = self._track_warnings.get(family, 0) + 1
                if kind:
                    # Warnings, lap invalidations and retirements also arrive as PENA
                    # with time_s = 255; only real penalties are announced.
                    time_s = int(pkt.detail.get("time_s", 0))
                    self._last_penalty_st = pkt.header.session_time
                    self.penalty_type = ptype
                    self.penalty_kind = kind
                    self.penalty_infringement = int(pkt.detail.get("infringement_type", 0))
                    self.penalty_time_s = time_s if kind == "time" and time_s != 255 else 0
                    if self.penalty_time_s:
                        if (
                            self._penalty_lap_change_st is not None
                            and self._penalty_lap_change_st >= pkt.header.session_time
                            and self._penalty_lap_increase_s >= self.penalty_time_s
                        ):
                            self._penalty_lap_increase_s -= self.penalty_time_s
                        else:
                            self._penalty_pending_s = (
                                max(self.penalty_s, self._penalty_pending_s) + self.penalty_time_s
                            )
        elif pkt.code == "BUTN":
            status = pkt.detail.get("button_status", 0) if isinstance(pkt.detail, dict) else 0
            if self._press_bit is not None:
                down = bool(status & self._press_bit)
                if down != self._press_down:
                    self._press_down = down
                    for cb in self.press_listeners:
                        cb(recv_time, down)
            if self._toggle_bit is not None:
                tdown = bool(status & self._toggle_bit)
                if tdown and not self._toggle_down:
                    for tcb in self.toggle_listeners:
                        tcb(recv_time)
                self._toggle_down = tdown
            for kind, bit in self._action_bits.items():
                adown = bool(status & bit)
                if adown and not self._action_down.get(kind, False):
                    for acb in self.action_listeners:
                        acb(kind, recv_time)
                self._action_down[kind] = adown

    def _on_session_history(self, pkt: SessionHistoryPacket) -> None:
        self._histories[pkt.car_idx] = pkt
        laps = pkt.laps[: pkt.num_laps]
        best_lap = min(
            (lap for lap in laps if lap.lap_valid_bit_flags & LAP_VALID and lap.lap_time_ms),
            key=lambda lap: lap.lap_time_ms,
            default=None,
        )
        self._best_laps[pkt.car_idx] = best_lap.lap_time_ms if best_lap is not None else 0
        if pkt.car_idx != self._player_idx:
            self._emit_rival_laps(pkt)
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

    def _emit_rival_laps(self, pkt: SessionHistoryPacket) -> None:
        """Expose newly completed rival laps as LapSummary rows. Completed
        laps are indices < num_laps - 1 (the last entry is in progress)."""
        completed = max(0, pkt.num_laps - 1)
        emitted = self._rival_laps_emitted.get(pkt.car_idx, 0)
        if completed <= emitted:
            return
        stints = pkt.tyre_stints[: pkt.num_tyre_stints]
        # Persisted laps keep car_idx 0 for the player; a rival in slot 0 takes
        # the player's (otherwise unused) slot instead.
        slot = self._player_idx if pkt.car_idx == 0 else pkt.car_idx
        for i in range(emitted, completed):
            lap = pkt.laps[i]
            lap_num = i + 1
            compound = visual = 0
            stint_start = 1
            for stint in stints:
                compound = stint.tyre_actual_compound
                visual = stint.tyre_visual_compound
                if stint.end_lap >= lap_num:
                    break
                stint_start = stint.end_lap + 1
            valid = bool(lap.lap_valid_bit_flags & LAP_VALID) and lap.lap_time_ms > 0
            self.rival_laps.append(
                (
                    slot,
                    LapSummary(
                        lap_num=lap_num,
                        lap_time_ms=lap.lap_time_ms,
                        sector1_ms=lap.sector1_ms,
                        sector2_ms=lap.sector2_ms,
                        compound=compound,
                        visual=visual,
                        tyre_age_laps=max(0, lap_num - stint_start),
                        fuel_remaining_laps_at_end=0.0,
                        valid=valid,
                        invalid_reasons=[] if valid else ["invalid"],
                    ),
                )
            )
        self._rival_laps_emitted[pkt.car_idx] = completed

    def _note_lap_boundary(self) -> None:
        """Player crossed the line: evaluate restricted-telemetry streak."""
        status = self.cars_status
        damage = self.cars_damage
        lap = self.cars_lap
        if status is None or damage is None or lap is None:
            return
        active = [i for i, c in enumerate(lap) if i != self._player_idx and c.result_status == 2]
        all_zero = bool(active) and all(
            float(status[i].fuel_in_tank) == 0.0
            and float(status[i].ers_store_energy) == 0.0
            and sum(damage[i].tyres_wear.as_tuple()) == 0.0
            for i in active
            if i < len(status) and i < len(damage)
        )
        if all_zero and len(active) <= sum(
            1 for i in active if i < len(status) and i < len(damage)
        ):
            self._restricted_streak += 1
        else:
            self._restricted_streak = 0
        self.rival_data_restricted = self._restricted_streak >= int(
            self._th("restricted_detect_laps", 2.0)
        )

    def _note_corner_wear(self) -> None:
        """Per-corner wear rate over the last few stint laps vs the other three.

        Sets `wear_hot_corner` to a corner name, or "fronts"/"rears" when both
        tyres on one axle outpace the other axle.
        """
        wear = self.tyres_wear.as_tuple()
        marks = self._wear_marks
        if marks and any(w < m - 1.0 for w, m in zip(wear, marks[-1], strict=True)):
            marks.clear()
        marks.append(wear)
        window = max(1, int(self._th("wear_corner_window_laps", 3)))
        del marks[: -(window + 1)]
        self.wear_hot_corner = ""
        if len(marks) <= window:
            return
        rates = [(a - b) / window for a, b in zip(marks[-1], marks[0], strict=True)]
        ratio_min = self._th("wear_corner_ratio", 1.4)
        if self.wear_hot_ratio >= ratio_min:
            ratio_min -= self._th("wear_corner_hysteresis", 0.15)
        delta_min = self._th("wear_corner_min_delta_pct", 0.6)
        front, rear = (rates[2] + rates[3]) / 2, (rates[0] + rates[1]) / 2
        axles = (("fronts", front, rear, (2, 3)), ("rears", rear, front, (0, 1)))
        for name, hi, lo, idx in axles:
            same_axle = min(rates[i] for i in idx) >= ratio_min * lo
            if lo > 0 and same_axle and hi - lo >= delta_min:
                self._set_wear_hot(name, hi / lo, hi, max(wear[i] for i in idx))
                return
        i = max(range(4), key=rates.__getitem__)
        others = (sum(rates) - rates[i]) / 3
        if others > 0 and rates[i] >= ratio_min * others and rates[i] - others >= delta_min:
            self._set_wear_hot(_CORNER_WORDS[i], rates[i] / others, rates[i], wear[i])
        else:
            self.wear_hot_ratio = 0.0

    def _set_wear_hot(self, corner: str, ratio: float, rate: float, pct: float) -> None:
        self.wear_hot_corner = corner
        self.wear_hot_ratio = ratio
        self.wear_hot_rate = rate
        self.wear_hot_pct = pct

    def _race_horizon_min(self) -> float:
        """Minutes of racing left, so forecast samples after the flag don't
        drive a tyre call. 30 (the forecast's reach) when unknown."""
        lap_ms = self._best_laps.get(self._player_idx, 0)
        if self._kind() != "race" or self.total_laps <= 0 or lap_ms <= 0:
            return 30.0
        laps_left = max(0, self.total_laps - self.lap_num + 1)
        return laps_left * lap_ms / 60_000.0

    def _update_weather_crossover(self) -> None:
        """Crossover direction from the forecast, with % hysteresis so it
        doesn't flap around the thresholds."""
        horizon = self._race_horizon_min()
        ahead = [
            (int(f.rain_percentage), int(f.time_offset))
            for f in self._forecast_samples
            if f.session_type == self.session_type
            and 5 <= f.time_offset
            and (f.time_offset <= horizon or f.time_offset <= 10)
        ]
        rain, rain_min = max(ahead, default=(0, 0))
        self._weather_crossover_pct, self._weather_crossover_min = rain, rain_min
        wet = self._th("rain_wet_pct", 85.0)
        inter = self._th("rain_inter_pct", 60.0)
        dry = self._th("rain_dry_pct", 30.0)
        hyst = self._th("rain_hysteresis_pct", 5.0)
        cur = self._weather_crossover
        # Wetter states need the forecast past the band edge; clearing a
        # state needs it back below the band edge (hysteresis).
        if rain >= wet + (0.0 if cur == "to_inter" else hyst):
            cand = "to_wet"
        elif rain >= inter + (0.0 if cur == "to_wet" else hyst):
            cand = "to_inter"
        elif (self.weather >= 3 or self.tyre_compound in (7, 8)) and rain < dry - (
            0.0 if cur else hyst
        ):
            cand = "to_dry"
        elif cur in ("to_inter", "to_wet") and rain < inter - hyst:
            cand = ""
        else:
            cand = cur
        self._weather_crossover = cand

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
        self.setup_on_throttle_diff = car.on_throttle
        self.setup_off_throttle_diff = car.off_throttle
        self.setup = dataclasses.asdict(car)
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
        self.lap_acc.note_tyre_temperatures(
            car.tyres_inner_temperature.as_tuple(), car.tyres_surface_temperature.as_tuple()
        )
        self.brake_temp = car.brakes_temperature
        self.speed_kmh = float(car.speed)
        self.off_track.update_surface(st, car.surface_type.as_tuple())
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
        if self.tyre_compound and car.actual_tyre_compound != self.tyre_compound:
            self._on_tyre_change()
        self.tyre_compound = car.actual_tyre_compound
        self.tyre_visual = car.visual_tyre_compound
        self.tyre_age_laps = car.tyres_age_laps
        self.fuel_remaining_laps = car.fuel_remaining_laps
        self.fuel_in_tank = car.fuel_in_tank
        cap = 4_000_000.0  # nominal 4 MJ ERS store
        self.ers_store_energy_j = float(car.ers_store_energy)
        self.ers_store_pct = min(100.0, car.ers_store_energy / cap * 100.0)
        self.ers_deployed_this_lap_j = float(car.ers_deployed_this_lap)
        self.ers_harvested_mguk_j = float(car.ers_harvested_this_lap_mguk)
        self.ers_harvested_mguh_j = float(car.ers_harvested_this_lap_mguh)
        self.ers_deploy_mode = car.ers_deploy_mode
        self.drs_allowed = car.drs_allowed
        self.vehicle_fia_flags = car.vehicle_fia_flags
        self.network_paused = bool(car.network_paused)

    def _on_tyre_change(self) -> None:
        """A new set is on: restart the tyre EMAs so the old set's heat is not
        read against the new compound's window."""
        for ema in (
            self.tyre_inner_fast,
            self.tyre_inner_slow,
            self.tyre_surface_fast,
            self.tyre_surface_slow,
        ):
            ema.reset()
        self._overheat = False
        self._graining = False

    def _on_car_damage(self, pkt: CarDamagePacket) -> None:
        self.cars_damage = pkt.cars
        car = pkt.cars[self._player_idx]
        self.tyres_wear = car.tyres_wear
        self.blister_max_pct = max(int(b) for b in car.tyre_blisters.as_tuple())
        gap = self._th("puncture_gap_pct", 40)
        self.puncture_corner = next(
            (
                name
                for name, dmg, wear in zip(
                    _CORNER_WORDS,
                    car.tyres_damage.as_tuple(),
                    car.tyres_wear.as_tuple(),
                    strict=True,
                )
                if dmg - wear >= gap
            ),
            "",
        )
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

    def _damage_parts(self) -> dict[str, int]:
        d = self.damage
        return {
            "front left wing": d.front_left_wing,
            "front right wing": d.front_right_wing,
            "rear wing": d.rear_wing,
            "floor": d.floor,
            "diffuser": d.diffuser,
            "sidepod": d.sidepod,
            "gearbox": d.gearbox,
            "engine": d.engine,
        }

    def _warn_pct(self, part: str) -> float:
        if part.startswith("front"):
            return self._th("front_wing_damage_warn_pct", 15.0)
        if part in ("gearbox", "engine"):
            return self._th("powertrain_damage_warn_pct", 30.0)
        return self._th("damage_warn_pct", 20.0)

    def _contact_view(self, st: float) -> dict[str, Any]:
        c = self.contacts
        phase = c.phase(st, self._th("contact_check_s", 4.0), self._th("contact_report_s", 10.0))
        if not phase:
            return dict(contact_episodes=c.episodes)
        part, pct = c.worst_new(self._damage_parts(), int(self._th("contact_damage_min_pct", 3.0)))
        warn = self._warn_pct(part) if part else math.inf
        me, other = self._player_idx, c.other
        parts = self.participants
        mate = (
            0 <= other < len(parts)
            and me < len(parts)
            and parts[other].team_id == parts[me].team_id != 255
        )
        return dict(
            contact_phase=phase,
            contact_name=parts[other].name if 0 <= other < len(parts) else "",
            contact_teammate=mate,
            contact_hits=c.hits,
            contact_episodes=c.episodes,
            contact_damage=part,
            contact_damage_pct=pct,
            contact_damage_major=pct >= warn,
        )

    def _teammate(self) -> int:
        parts = self.participants
        me = self._player_idx
        if me >= len(parts) or parts[me].team_id == 255:
            return -1
        team = parts[me].team_id
        return next((i for i, p in enumerate(parts) if i != me and p.team_id == team), -1)

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
                    total_distance=c.total_distance,
                    current_lap_time_ms=c.current_lap_time_ms,
                    last_lap_time_ms=c.last_lap_time_ms,
                    sector=c.sector,
                    sector1_ms=c.sector1_ms,
                    sector2_ms=c.sector2_ms,
                    car_position=c.car_position,
                    driver_status=c.driver_status,
                    pit_status=c.pit_status,
                    result_status=c.result_status,
                    current_lap_num=c.current_lap_num,
                    delta_to_car_in_front_ms=c.delta_to_car_in_front_ms,
                    delta_to_race_leader_ms=c.delta_to_race_leader_ms,
                    num_pit_stops=c.num_pit_stops,
                    penalties=c.penalties,
                    total_warnings=c.total_warnings,
                    corner_cutting_warnings=c.corner_cutting_warnings,
                    num_unserved_drive_through_pens=c.num_unserved_drive_through_pens,
                    num_unserved_stop_go_pens=c.num_unserved_stop_go_pens,
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
                    ref_sectors=next(
                        (
                            self._best_lap_sectors.get(i, (0, 0, 0))
                            for i, b in enumerate(field_best)
                            if b == cutoff_ms and i != self._player_idx
                        ),
                        (0, 0, 0),
                    ),
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
        # Advice is against the setup the run was driven on, so it stays put
        # while the driver dials the new pressures in.
        if phase in ("in_lap", "pitting", "garage"):
            if self._pressure_base is None:
                self._pressure_base = self.setup_tyre_pressure
        else:
            self._pressure_base = None
        pressure_low, pressure_high = pressure_window(self._thresholds, self.tyre_compound)
        pressures = pressure_advice(
            run_avg,
            self._pressure_base or self.setup_tyre_pressure,
            pressure_low,
            pressure_high,
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
        race = self._race_view(st, kind, cars, field_best)
        laps_remaining = max(0, self.total_laps - self.lap_num + 1) if self.total_laps > 0 else 0
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
            weekend_link=self.weekend_link_identifier,
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
            weather=self.weather,
            game_mode=self.game_mode,
            laps_remaining=laps_remaining,
            lights_out=self.lights_out,
            chequered=self.chequered,
            gap_ahead_s=race.pop(
                "gap_ahead_s",
                self.delta_to_car_in_front_ms / 1000.0
                if self.delta_to_car_in_front_ms > 0
                else math.inf,
            ),
            blister_max_pct=self.blister_max_pct,
            wear_hot_corner=self.wear_hot_corner,
            wear_hot_ratio=self.wear_hot_ratio,
            wear_hot_rate=self.wear_hot_rate,
            wear_hot_pct=self.wear_hot_pct,
            wear_mean_pct=sum(self.tyres_wear.as_tuple()) / 4.0,
            wear_max_pct=max(self.tyres_wear.as_tuple()),
            unserved_drive_through=self.unserved_drive_through,
            unserved_stop_go=self.unserved_stop_go,
            warnings=self.warnings,
            corner_cut_warnings=self.corner_cut_warnings,
            penalty_s=max(self.penalty_s, self._penalty_pending_s),
            penalty_type=self.penalty_type,
            penalty_infringement=self.penalty_infringement,
            penalty_time_s=self.penalty_time_s,
            penalty_kind=self.penalty_kind,
            penalty_recent=(
                self._last_penalty_st is not None
                and 0.0 <= st - self._last_penalty_st <= self._th("penalty_recent_s", 10.0)
            ),
            track_warning_kind=self.track_warning_kind,
            track_warning_count=self._track_warnings.get(
                _warning_family(self.track_warning_kind), 0
            ),
            track_warning_recent=(
                self._last_track_warning_st is not None
                and 0.0 <= st - self._last_track_warning_st <= self._th("penalty_recent_s", 10.0)
            ),
            blue_flag=self.vehicle_fia_flags == 4,
            weather_now=self.weather,
            rain_pct_now=self._rain_at(0),
            rain_pct_in_10=self._rain_at(10),
            rain_pct_in_30=self._rain_at(30),
            weather_in_10=self._weather_at(10),
            weather_in_30=self._weather_at(30),
            weather_crossover=self._weather_crossover,
            weather_crossover_pct=self._weather_crossover_pct,
            weather_crossover_min=self._weather_crossover_min,
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
            player_last_lap_ms=(
                cars[self._player_idx].last_lap_time_ms if 0 <= self._player_idx < len(cars) else 0
            ),
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
            setup_on_throttle_diff=self.setup_on_throttle_diff,
            setup_off_throttle_diff=self.setup_off_throttle_diff,
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
            front_wing_pit_status=(
                "box"
                if max(self.damage.front_left_wing, self.damage.front_right_wing)
                >= self._th("front_wing_lost_pct", 50.0)
                and laps_remaining > self._th("pit_min_laps_left", 2.0)
                else "nurse"
                if max(self.damage.front_left_wing, self.damage.front_right_wing)
                >= self._th("front_wing_lost_pct", 50.0)
                else "review"
                if max(self.damage.front_left_wing, self.damage.front_right_wing)
                >= self._th("front_wing_damage_warn_pct", 15.0)
                else ""
            ),
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
            saved=self.saves.recent(st) and not self.spins.recent(st),
            saves=self.saves.count,
            save_peak_deg=round(self.saves.peak_deg),
            off_track_lost=self.off_track.lost_recent(st),
            off_track_recovered=self.off_track.recovered_recent(st),
            is_sprint=self._is_sprint(),
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
            setup=dict(self.setup),
            run_tyre_inner_avg=run_avg,
            run_flying_s=self.run_temps.seconds,
            pressure_advice=pressures,
            pressure_advice_text=pressure_text(pressures),
            **cool,
            **race,
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
        low_c, high_c = pressure_window(self._thresholds, self.tyre_compound)
        if temps[hot_i] > high_c:
            hint = f"{WHEEL_NAMES[hot_i]} {temps[hot_i]:.0f}, off the kerbs"
        elif min(temps) < low_c:
            cold_i = min(range(4), key=lambda i: temps[i])
            hint = f"{WHEEL_NAMES[cold_i]} {temps[cold_i]:.0f}, keep heat in it"
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
            why = "No fuel for another lap"
        elif plan.reason == "flag":
            why = "That's the flag"
        elif plan.reason == "invalid":
            why = "Next lap's gone too, track limits"
        elif plan.reason == "safe":
            why = "You're safe"
        pole_idx = min(
            (i for i, b in enumerate(field_best) if b > 0), key=lambda i: field_best[i], default=-1
        )
        pole_gap = 0
        gaps = (0, 0, 0)
        name = ""
        mine = self._best_lap_sectors.get(self._player_idx, (0, 0, 0))
        theirs: tuple[int, int, int] = (0, 0, 0)
        if pole_idx >= 0 and pole_idx != self._player_idx and player_best > 0:
            pole_gap = player_best - field_best[pole_idx]
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
            next_lap_invalid=run.next_lap_invalid,
            ers_need_pct=round(self._ers_need_pct()),
            cool_extend=cool_lap and self._cool_extend(),
            time_for_cool_and_hot=self._time_for_cool_and_hot(),
            time_for_out_lap=self._time_for_out_lap(),
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
            best_sectors_ms=mine,
            pole_sectors_ms=theirs,
            pole_worst_sector=worst + 1 if gaps[worst] > 0 else 0,
            pole_worst_sector_s=round(max(0, gaps[worst]) / 1000.0, 1),
            hot_car_behind_s=(
                self._hot_car_behind_s() if cool_lap or phase == "out_lap" else math.inf
            ),
        )
        return out

    def _weather_at(self, offset_min: int) -> int:
        """Forecast weather type of the nearest sample at time_offset >= offset_min."""
        return next(
            (
                int(s.weather)
                for s in self._forecast_samples
                if s.session_type == self.session_type and s.time_offset >= offset_min
            ),
            -1,
        )

    def _rain_at(self, offset_min: int) -> int:
        """Rain chance % of the nearest forecast sample for this session type at
        time_offset >= offset_min (0 = current conditions sample)."""
        return next(
            (
                int(s.rain_percentage)
                for s in self._forecast_samples
                if s.session_type == self.session_type and s.time_offset >= offset_min
            ),
            0,
        )

    def _rival_age(self, car_idx: int) -> int:
        """Tyre age of the rival's current stint from its Session History."""
        if self.rival_data_restricted:
            return 0
        pkt = self._histories.get(car_idx)
        if pkt is None or pkt.num_tyre_stints == 0:
            return 0
        stints = pkt.tyre_stints[: pkt.num_tyre_stints]
        prev_end = stints[-2].end_lap if len(stints) >= 2 else 0
        return int(max(0, pkt.num_laps - prev_end))

    def _fastest_lap_view(self, st: float, field_best: tuple[int, ...]) -> dict[str, Any]:
        if self._fastest_lap is None:
            return {}
        idx, ms, at = self._fastest_lap
        name = self.participants[idx].name if 0 <= idx < len(self.participants) else ""
        mine = idx == self._player_idx
        best = field_best[self._player_idx] if 0 <= self._player_idx < len(field_best) else 0
        secs = round(ms / 100) / 10
        return dict(
            fastest_lap_ms=ms,
            fastest_lap_mine=mine,
            fastest_lap_name=name,
            fastest_lap_time=f"{int(secs // 60)}:{secs % 60:04.1f}",
            fastest_lap_spoken=spoken_lap_time(ms),
            fastest_lap_age_s=max(0.0, st - at),
            fastest_lap_gap_s=(best - ms) / 1000.0 if best > 0 and not mine else math.inf,
        )

    def _race_view(
        self,
        st: float,
        kind: str,
        cars: tuple[CarLap, ...],
        field_best: tuple[int, ...],
    ) -> dict[str, Any]:
        """M3 race fields of the snapshot (docs/18)."""
        overheat, graining = self._thermal()
        model = self._model
        base: dict[str, Any] = dict(
            race_phase=self.race_phase,
            sc_laps=self.sc_laps,
            grid_position=self.grid_position,
            positions_gained=(
                self.grid_position - self.position if self.grid_position and self.position else 0
            ),
            gap_ahead_s=(
                self.delta_to_car_in_front_ms / 1000.0
                if kind != "race" and self.delta_to_car_in_front_ms > 0
                else math.inf
            ),
            sc_ending=self.sc_ending and self.race_phase in ("sc", "vsc"),
            **self._fastest_lap_view(st, field_best),
            neutral_ended_s=max(0.0, (self._last_session_time or 0.0) - self._race.neutral_end_t),
            neutral_ended_kind=self._race.neutral_end_kind,
            puncture_corner=self.puncture_corner,
            **self._contact_view(st),
            deg_fit_source=model.deg_fit_source,
            deg_ms_per_lap=model.deg_ms_per_lap,
            deg_confidence=model.deg_confidence,
            base_pace_ms=model.base_pace_ms,
            laps_of_pace=model.laps_of_pace,
            wear_per_lap_pct=model.wear_per_lap_pct,
            pit_loss_s=model.pit_loss_s,
            pit_loss_source=model.pit_loss_source,
            fuel_margin_laps=model.fuel_margin_laps,
            fuel_per_lap_kg=model.fuel_per_lap_kg,
            fuel_source=model.fuel_source,
            energy_per_lap_mj=model.energy_per_lap_mj,
            energy_lap_delta_mj=model.energy_lap_delta_mj,
            energy_laps_to_floor=model.energy_laps_to_floor,
            energy_mode=model.energy_mode,
            predicted_lap_ms=model.predicted_lap_ms,
            pit_plan=model.pit_plan,
            pit_plan_lap=model.pit_plan_lap,
            pit_plan_gain_s=model.pit_plan_gain_s,
            pit_plan_confidence=model.pit_plan_confidence,
            pit_plan_risk=model.pit_plan_risk,
            pit_plan_rival_idx=model.pit_plan_rival_idx,
            pit_plan_rival_name=model.pit_plan_rival_name,
            pit_plan_reason=model.pit_plan_reason,
            pit_window_start=model.pit_window_start,
            pit_window_end=model.pit_window_end,
            undercut_s=model.undercut_s,
            overcut_s=model.overcut_s,
            plans=model.plans,
            active_plan=model.active_plan,
            on_plan=model.on_plan,
            plan_label=model.plan_label,
            plan_spoken=model.plan_spoken,
            plan_stops_left=model.plan_stops_left,
            plan_target_lap=model.plan_target_lap,
            plan_window_start=model.plan_window_start,
            plan_window_end=model.plan_window_end,
            plan_window_text=model.plan_window_text,
            plan_window_open=model.plan_window_open,
            plan_next_compound=model.plan_next_compound,
            plan_off_s=model.plan_off_s,
            plan_switch_count=model.plan_switch_count,
            plan_switched_from=model.plan_switched_from,
            plan_switch_reason=model.plan_switch_reason,
            plan_switch_lap=model.plan_switch_lap,
            plan_target_shift=model.plan_target_shift,
            plan_b_spoken=model.plan_b_spoken,
            plan_b_delta_s=model.plan_b_delta_s,
            plan_c_spoken=model.plan_c_spoken,
            plan_c_delta_s=model.plan_c_delta_s,
            rival_data_restricted=self.rival_data_restricted,
            overheat=overheat,
            graining=graining,
            drs_zone_ahead=self._drs_zone_ahead(),
        )
        if kind != "race" or not cars or self.track_length_m <= 0:
            base["race_phase"] = self.race_phase if kind == "race" else ""
            return base

        # Player pace = median of the last 3 valid own laps (best-lap
        # fallback); pit-exit rival projected by the docs/03 formula.
        own = [lap.lap_time_ms for lap in self.laps if lap.valid and lap.lap_time_ms > 0]
        pace_ms = int(median(own[-3:])) if own else self._best_laps.get(self._player_idx, 0)
        ref_speed = (
            self.track_length_m / (pace_ms / 1000.0)
            if pace_ms > 0
            else self.track_length_m / self._th("release_fallback_lap_s", 95.0)
        )
        pit_s = model.pit_loss_s if model.pit_loss_s > 0 else self._th("pit_loss_default_s", 22.0)
        metres_lost = self.track_length_m * pit_s / (pace_ms / 1000.0) if pace_ms > 0 else math.inf
        pen_pos, pen_margin, pen_i = penalty_standing(
            cars, self._player_idx, self.track_length_m, speed_mps=ref_speed
        )
        ahead_i, behind_i, exit_i = relevant_rivals(
            cars,
            self._player_idx,
            math.inf,
            (self.lap_distance, self.track_length_m, metres_lost),
            ref_speed_mps=ref_speed,
        )
        if ahead_i >= 0:
            gap_ahead = self.delta_to_car_in_front_ms / 1000.0
            self._ahead_latch = (ahead_i, gap_ahead)
        else:
            latch_i, latch_gap = self._ahead_latch
            if (
                0 <= latch_i < len(cars)
                and cars[latch_i].pit_status != 0
                and 0 < cars[latch_i].car_position < self.position
            ):
                ahead_i, gap_ahead = latch_i, latch_gap
            else:
                self._ahead_latch = (-1, math.inf)
                gap_ahead = math.inf
        # gap_behind = the car behind's delta to the car in front of it (us).
        gap_behind = math.inf
        if behind_i >= 0:
            d = cars[behind_i].delta_to_car_in_front_ms
            gap_behind = d / 1000.0 if d > 0 else math.inf

        def pace_of(i: int) -> int:
            h = self._histories.get(i)
            if h is None:
                return self._best_laps.get(i, 0)
            return rival_pace_ms(
                h,
                int(self._th("rival_pace_window", 3.0)),
                self._th("rival_pace_outlier_ratio", 1.07),
            ) or self._best_laps.get(i, 0)

        def name_of(i: int) -> str:
            return self.participants[i].name if 0 <= i < len(self.participants) else ""

        pitted = self._cars_pitted_this_lap | self._pitted_lap_snapshot
        # Pit-exit clean air: reuse the quali release window with the pit
        # loss as the out-lap and the race clean-gap threshold.
        release = release_window(
            cars,
            self._player_idx,
            self.track_length_m,
            self._th("pit_exit_m", 0.0),
            pit_s,
            field_best,
            pace_ms / 1000.0 if pace_ms > 0 else self._th("release_fallback_lap_s", 95.0),
            self._th("pit_exit_clean_gap_s", 2.0),
        )
        exit_gap = math.inf
        if exit_i >= 0:
            v = self.track_length_m / (pace_of(exit_i) / 1000.0) if pace_of(exit_i) > 0 else 50.0
            fwd = (cars[exit_i].lap_distance - self._th("pit_exit_m", 0.0)) % self.track_length_m
            exit_gap = (
                fwd / v if fwd <= self.track_length_m / 2 else -((self.track_length_m - fwd) / v)
            )
        self._gap_now = {
            "ahead": (ahead_i, gap_ahead),
            "behind": (behind_i, gap_behind),
        }
        base["gap_ahead_s"] = gap_ahead

        def trend(side: str, idx: int) -> float:
            if len(self._gap_lines) < 2 or idx < 0:
                return 0.0
            a, b = self._gap_lines[0].get(side), self._gap_lines[1].get(side)
            if a is None or b is None or a[0] != idx or b[0] != idx:
                return 0.0
            if not (math.isfinite(a[1]) and math.isfinite(b[1])):
                return 0.0
            return round(a[1] - b[1], 3)

        status = self.cars_status

        def compound_of(i: int) -> int:
            if status is None or not 0 <= i < len(status):
                return 0
            return int(status[i].visual_tyre_compound)

        slick_gain = self._compound_gain(cars, (7,), (16, 17, 18))
        inter_gain = self._compound_gain(cars, (8,), (7,))
        base.update(
            rival_ahead_pos=cars[ahead_i].car_position if ahead_i >= 0 else 0,
            rival_behind_pos=cars[behind_i].car_position if behind_i >= 0 else 0,
            rival_ahead_compound=compound_of(ahead_i),
            rival_behind_compound=compound_of(behind_i),
            gap_trend_ahead_s=trend("ahead", ahead_i),
            gap_trend_behind_s=trend("behind", behind_i),
            gap_behind_s=gap_behind,
            rival_ahead_idx=ahead_i,
            rival_behind_idx=behind_i,
            rival_pit_exit_idx=exit_i,
            rival_ahead_pace_ms=pace_of(ahead_i) if ahead_i >= 0 else 0,
            rival_behind_pace_ms=pace_of(behind_i) if behind_i >= 0 else 0,
            rival_pit_exit_pace_ms=pace_of(exit_i) if exit_i >= 0 else 0,
            rival_ahead_name=name_of(ahead_i),
            rival_behind_name=name_of(behind_i),
            rival_pit_exit_name=name_of(exit_i),
            rival_ahead_age=self._rival_age(ahead_i) if ahead_i >= 0 else 0,
            rival_behind_age=self._rival_age(behind_i) if behind_i >= 0 else 0,
            rival_ahead_pitted=ahead_i in pitted if ahead_i >= 0 else False,
            rival_ahead_in_pit_lane=ahead_i >= 0 and cars[ahead_i].pit_status != 0,
            rival_behind_pitted=behind_i in pitted if behind_i >= 0 else False,
            pit_exit_rival_gap_s=exit_gap,
            **self._teammate_view(ahead_i, behind_i, gap_ahead, gap_behind, name_of),
            slick_gain_s=slick_gain,
            inter_gain_s=inter_gain,
            tyre_switch_to=self._tyre_switch(slick_gain, inter_gain),
            penalty_position=pen_pos,
            penalty_margin_s=pen_margin,
            penalty_threat_name=name_of(pen_i) if pen_i >= 0 else "",
            pit_exit_clean=release.clean,
            drs_available=(
                bool(self.drs_allowed)
                and self.delta_to_car_in_front_ms > 0
                and self.delta_to_car_in_front_ms / 1000.0 < self._th("drs_detection_gap_s", 1.0)
                and self.safety_car_status == 0
            ),
        )
        return base

    def _teammate_view(
        self,
        ahead_i: int,
        behind_i: int,
        gap_ahead: float,
        gap_behind: float,
        name_of: Callable[[int], str],
    ) -> dict[str, Any]:
        mate = self._teammate()
        if mate < 0:
            return {}
        gap = math.inf
        if mate == ahead_i:
            gap = gap_ahead
        elif mate == behind_i:
            gap = gap_behind
        return dict(
            teammate_name=name_of(mate),
            teammate_gap_s=gap,
            teammate_fight=self.race_phase == "racing"
            and gap <= self._th("teammate_fight_gap_s", 1.0),
        )

    def _compound_gain(
        self, cars: Sequence[CarLap], wetter: tuple[int, ...], drier: tuple[int, ...]
    ) -> float:
        """Median last lap on the wetter compounds minus the drier ones (s), from
        running cars with at least one full lap on their current set."""
        status = self.cars_status
        if status is None or self.race_phase != "racing" or self.safety_car_status != 0:
            return 0.0
        pitted = self._cars_pitted_this_lap | self._pitted_lap_snapshot
        groups: dict[bool, list[int]] = {True: [], False: []}
        for i, c in enumerate(cars):
            if i >= len(status) or i in pitted or c.pit_status != PitStatus.NONE:
                continue
            if c.result_status != 2 or c.last_lap_time_ms <= 0 or status[i].tyres_age_laps < 1:
                continue
            comp = int(status[i].visual_tyre_compound)
            if comp in wetter or comp in drier:
                groups[comp in drier].append(c.last_lap_time_ms)
        need = int(self._th("compound_gain_min_cars", 2.0))
        if len(groups[True]) < need or len(groups[False]) < need:
            return 0.0
        return round((median(groups[False]) - median(groups[True])) / 1000.0, 2)

    def _tyre_switch(self, slick_gain: float, inter_gain: float) -> str:
        """Compound the field's lap times say to switch to, else ''."""
        th = self._th("compound_crossover_s", 1.0)
        comp = self.tyre_compound
        if comp == 7 and slick_gain >= th:
            return "slicks"
        if comp == 7 and inter_gain <= -th:
            return "wets"
        if comp == 8 and inter_gain >= th:
            return "inters"
        if comp in (16, 17, 18, 19, 20, 21, 22) and slick_gain <= -th:
            return "inters"
        return ""

    def _drs_zone_ahead(self) -> bool:
        """A DRS/active-aero zone starts within the lookahead distance."""
        if self.track_length_m <= 0:
            return False
        lookahead = self._th("drs_zone_lookahead_m", 300.0)
        for zone in (*self._drs_zones, *self._aero_zones):
            dist = (float(zone.zone_start) - self.lap_distance) % self.track_length_m
            if 0 < dist <= lookahead:
                return True
        return False

    def _thermal(self) -> tuple[bool, bool]:
        """(overheat, graining) with hysteresis on the slow inner EMA."""
        temps = self._ema_or_zero(self.tyre_inner_slow).as_tuple()
        hot = max(temps)
        coldest = min(temps)
        hot_c = thermal_window(self._thresholds, self.tyre_compound)[1]
        hot_c += self._thermal_warn_offset_c
        hyst = self._th("thermal_hysteresis_c", 4.0)
        suffix = {7: "_inter", 8: "_wet"}.get(self.tyre_compound, "")
        grain_c = self._th(f"tyre_graining{suffix}_c", 75.0)
        on_track = self.pit_status == PitStatus.NONE
        self._overheat = on_track and ((self._overheat and hot > hot_c - hyst) or hot >= hot_c)
        in_context = self.race_phase == "racing" and self.tyre_age_laps >= 2
        self._graining = in_context and (
            (self._graining and coldest < grain_c + hyst) or coldest <= grain_c
        )
        return self._overheat, self._graining

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
        try:
            if SessionType(self.session_type).kind() == "race":
                return "red_flag" if self.red_flag else self.race_phase
        except ValueError:
            pass
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
