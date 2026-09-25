"""Declarative packet layouts, re-expressed as data from the format-2026 wire
structures. One table per packet id; compiled at import into struct.Struct
objects. Sizes are asserted against PACKET_SIZES — a mismatch means the table
(or the assumed header) is wrong, and it fails at import, loudly.

Layout items:
- Field(name, fmt, count=1, corners=False): a scalar or fixed array. fmt is a
  struct format code. count>1 yields a tuple; corners=True yields a Corners
  (wire order RL, RR, FL, FR).
- Array(name, layout, n): n nested sub-structures.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

CAR_SLOTS = 24


@dataclass(frozen=True)
class Field:
    name: str
    fmt: str
    count: int = 1
    corners: bool = False


@dataclass(frozen=True)
class Array:
    name: str
    layout: tuple[Item, ...]
    n: int


Item = Field | Array


@dataclass(frozen=True, slots=True)
class Corners:
    """Tyre-corner values. Wire order is RL, RR, FL, FR."""

    rl: Any
    rr: Any
    fl: Any
    fr: Any

    @property
    def RL(self) -> Any:
        return self.rl

    @property
    def RR(self) -> Any:
        return self.rr

    @property
    def FL(self) -> Any:
        return self.fl

    @property
    def FR(self) -> Any:
        return self.fr

    def as_tuple(self) -> tuple[Any, Any, Any, Any]:
        return (self.rl, self.rr, self.fl, self.fr)


@dataclass(slots=True)
class CompiledLayout:
    layout: tuple[Item, ...]
    struct: struct.Struct
    # byte offset of every Field/Array element within the layout, in order
    offsets: dict[str, int]

    def names(self) -> list[str]:
        return [item.name for item in self.layout]


def compile_layout(layout: tuple[Item, ...]) -> CompiledLayout:
    fmt = ["<"]
    offsets: dict[str, int] = {}
    pos = 0
    for item in layout:
        offsets[item.name] = pos
        if isinstance(item, Field):
            fmt.append(f"{item.count}{item.fmt}" if item.count > 1 else item.fmt)
            pos += struct.calcsize("<" + item.fmt) * item.count
        else:
            sub = compile_layout(item.layout)
            fmt.append(sub.struct.format[1:] * item.n)
            pos += sub.struct.size * item.n
    s = struct.Struct("".join(fmt))
    return CompiledLayout(layout=layout, struct=s, offsets=offsets)


def layout_size(layout: tuple[Item, ...]) -> int:
    return compile_layout(layout).struct.size


# ---------------------------------------------------------------- sub-layouts

MARSHAL_ZONE: tuple[Item, ...] = (
    Field("zone_start", "f"),
    Field("zone_flag", "b"),
)

WEATHER_FORECAST_SAMPLE: tuple[Item, ...] = (
    Field("session_type", "B"),
    Field("time_offset", "B"),
    Field("weather", "B"),
    Field("track_temperature", "b"),
    Field("track_temperature_change", "b"),
    Field("air_temperature", "b"),
    Field("air_temperature_change", "b"),
    Field("rain_percentage", "B"),
)

ACTIVE_AERO_ZONE: tuple[Item, ...] = (
    Field("zone_start", "f"),
    Field("zone_end", "f"),
)

DRS_ZONE = ACTIVE_AERO_ZONE

LAP_DATA_CAR: tuple[Item, ...] = (
    Field("last_lap_time_ms", "I"),
    Field("current_lap_time_ms", "I"),
    Field("sector1_time_ms_part", "H"),
    Field("sector1_time_minutes_part", "B"),
    Field("sector2_time_ms_part", "H"),
    Field("sector2_time_minutes_part", "B"),
    Field("delta_to_car_in_front_ms_part", "H"),
    Field("delta_to_car_in_front_minutes_part", "B"),
    Field("delta_to_race_leader_ms_part", "H"),
    Field("delta_to_race_leader_minutes_part", "B"),
    Field("lap_distance", "f"),
    Field("total_distance", "f"),
    Field("safety_car_delta", "f"),
    Field("car_position", "B"),
    Field("current_lap_num", "B"),
    Field("pit_status", "B"),
    Field("num_pit_stops", "B"),
    Field("sector", "B"),
    Field("current_lap_invalid", "B"),
    Field("penalties", "B"),
    Field("total_warnings", "B"),
    Field("corner_cutting_warnings", "B"),
    Field("num_unserved_drive_through_pens", "B"),
    Field("num_unserved_stop_go_pens", "B"),
    Field("grid_position", "B"),
    Field("driver_status", "B"),
    Field("result_status", "B"),
    Field("pit_lane_timer_active", "B"),
    Field("pit_lane_time_in_lane_ms", "H"),
    Field("pit_stop_timer_ms", "H"),
    Field("pit_stop_should_serve_pen", "B"),
    Field("speed_trap_fastest_speed", "f"),
    Field("speed_trap_fastest_lap", "B"),
)

CAR_TELEMETRY_CAR: tuple[Item, ...] = (
    Field("speed", "H"),
    Field("throttle", "f"),
    Field("steer", "f"),
    Field("brake", "f"),
    Field("clutch", "B"),
    Field("gear", "b"),
    Field("engine_rpm", "H"),
    Field("drs", "B"),
    Field("rev_lights_percent", "B"),
    Field("rev_lights_bit_value", "H"),
    Field("brakes_temperature", "H", 4, corners=True),
    Field("tyres_surface_temperature", "B", 4, corners=True),
    Field("tyres_inner_temperature", "B", 4, corners=True),
    Field("engine_temperature", "B"),
    Field("tyres_pressure", "f", 4, corners=True),
    Field("surface_type", "B", 4, corners=True),
)

CAR_STATUS_CAR: tuple[Item, ...] = (
    Field("traction_control", "B"),
    Field("anti_lock_brakes", "B"),
    Field("fuel_mix", "B"),
    Field("front_brake_bias", "B"),
    Field("pit_limiter_status", "B"),
    Field("fuel_in_tank", "f"),
    Field("fuel_capacity", "f"),
    Field("fuel_remaining_laps", "f"),
    Field("max_rpm", "H"),
    Field("idle_rpm", "H"),
    Field("max_gears", "B"),
    Field("drs_allowed", "B"),
    Field("drs_activation_distance", "H"),
    Field("actual_tyre_compound", "B"),
    Field("visual_tyre_compound", "B"),
    Field("tyres_age_laps", "B"),
    Field("vehicle_fia_flags", "b"),
    Field("engine_power_ice", "f"),
    Field("engine_power_mguk", "f"),
    Field("ers_store_energy", "f"),
    Field("ers_deploy_mode", "B"),
    Field("ers_harvested_this_lap_mguk", "f"),
    Field("ers_harvested_this_lap_mguh", "f"),
    Field("ers_harvest_limit_per_lap", "f"),
    Field("ers_deployed_this_lap", "f"),
    Field("network_paused", "B"),
)

CAR_DAMAGE_CAR: tuple[Item, ...] = (
    Field("tyres_wear", "f", 4, corners=True),
    Field("tyres_damage", "B", 4, corners=True),
    Field("brakes_damage", "B", 4, corners=True),
    Field("tyre_blisters", "B", 4, corners=True),
    Field("front_left_wing_damage", "B"),
    Field("front_right_wing_damage", "B"),
    Field("rear_wing_damage", "B"),
    Field("floor_damage", "B"),
    Field("diffuser_damage", "B"),
    Field("sidepod_damage", "B"),
    Field("drs_fault", "B"),
    Field("ers_fault", "B"),
    Field("gearbox_damage", "B"),
    Field("engine_damage", "B"),
    Field("engine_mguh_wear", "B"),
    Field("engine_es_wear", "B"),
    Field("engine_ce_wear", "B"),
    Field("engine_ice_wear", "B"),
    Field("engine_mguk_wear", "B"),
    Field("engine_tc_wear", "B"),
    Field("engine_blown", "B"),
    Field("engine_seized", "B"),
)

# ------------------------------------------------------------- packet layouts

SESSION_LAYOUT: tuple[Item, ...] = (
    Field("weather", "B"),
    Field("track_temperature", "b"),
    Field("air_temperature", "b"),
    Field("total_laps", "B"),
    Field("track_length", "H"),
    Field("session_type", "B"),
    Field("track_id", "b"),
    Field("formula", "B"),
    Field("session_time_left", "H"),
    Field("session_duration", "H"),
    Field("pit_speed_limit", "B"),
    Field("game_paused", "B"),
    Field("is_spectating", "B"),
    Field("spectator_car_index", "B"),
    Field("sli_pro_native_support", "B"),
    Field("num_marshal_zones", "B"),
    Array("marshal_zones", MARSHAL_ZONE, 21),
    Field("safety_car_status", "B"),
    Field("network_game", "B"),
    Field("num_weather_forecast_samples", "B"),
    Array("weather_forecast_samples", WEATHER_FORECAST_SAMPLE, 64),
    Field("forecast_accuracy", "B"),
    Field("ai_difficulty", "B"),
    Field("season_link_identifier", "I"),
    Field("weekend_link_identifier", "I"),
    Field("session_link_identifier", "I"),
    Field("pit_stop_window_ideal_lap", "B"),
    Field("pit_stop_window_latest_lap", "B"),
    Field("pit_stop_rejoin_position", "B"),
    Field("steering_assist", "B"),
    Field("braking_assist", "B"),
    Field("gearbox_assist", "B"),
    Field("pit_assist", "B"),
    Field("pit_release_assist", "B"),
    Field("ers_assist", "B"),
    Field("drs_assist", "B"),
    Field("dynamic_racing_line", "B"),
    Field("dynamic_racing_line_type", "B"),
    Field("game_mode", "B"),
    Field("rule_set", "B"),
    Field("time_of_day", "I"),
    Field("session_length", "B"),
    Field("speed_units_lead_player", "B"),
    Field("temperature_units_lead_player", "B"),
    Field("speed_units_secondary_player", "B"),
    Field("temperature_units_secondary_player", "B"),
    Field("num_safety_car_periods", "B"),
    Field("num_virtual_safety_car_periods", "B"),
    Field("num_red_flag_periods", "B"),
    Field("equal_car_performance", "B"),
    Field("recovery_mode", "B"),
    Field("flashback_limit", "B"),
    Field("surface_type", "B"),
    Field("low_fuel_mode", "B"),
    Field("race_starts", "B"),
    Field("tyre_temperature", "B"),
    Field("pit_lane_tyre_sim", "B"),
    Field("car_damage", "B"),
    Field("car_damage_rate", "B"),
    Field("collisions", "B"),
    Field("collisions_off_for_first_lap_only", "B"),
    Field("mp_unsafe_pit_release", "B"),
    Field("mp_off_for_griefing", "B"),
    Field("corner_cutting_stringency", "B"),
    Field("parc_ferme_rules", "B"),
    Field("pit_stop_experience", "B"),
    Field("safety_car", "B"),
    Field("safety_car_experience", "B"),
    Field("formation_lap", "B"),
    Field("formation_lap_experience", "B"),
    Field("red_flags", "B"),
    Field("affects_licence_level_solo", "B"),
    Field("affects_licence_level_mp", "B"),
    Field("num_sessions_in_weekend", "B"),
    Field("weekend_structure", "B", 12),
    Field("sector2_lap_distance_start", "f"),
    Field("sector3_lap_distance_start", "f"),
    Field("active_aero_track_status", "B"),
    Field("num_active_aero_zones_full", "B"),
    Array("active_aero_zones_full", ACTIVE_AERO_ZONE, 8),
    Field("num_active_aero_zones_partial", "B"),
    Array("active_aero_zones_partial", ACTIVE_AERO_ZONE, 8),
    Field("num_drs_zones", "B"),
    Array("drs_zones", DRS_ZONE, 4),
    Field("start_reaction_time", "f"),
    Field("anti_lock_brakes_assist", "B"),
    Field("traction_control_assist", "B"),
    Field("dynamic_racing_line_hi_vis", "B"),
    Field("dynamic_racing_line_colour_blind", "B"),
    Field("recurring_rewind_prompt", "B"),
)

LAP_DATA_LAYOUT: tuple[Item, ...] = (
    Array("cars", LAP_DATA_CAR, CAR_SLOTS),
    Field("time_trial_pb_car_idx", "B"),
    Field("time_trial_rival_car_idx", "B"),
)

EVENT_LAYOUT: tuple[Item, ...] = (
    Field("event_string_code", "4s"),
    Field("event_data", "12s"),
)

CAR_TELEMETRY_LAYOUT: tuple[Item, ...] = (
    Array("cars", CAR_TELEMETRY_CAR, CAR_SLOTS),
    Field("mfd_panel_index", "B"),
    Field("mfd_panel_index_secondary_player", "B"),
    Field("suggested_gear", "b"),
)

CAR_STATUS_LAYOUT: tuple[Item, ...] = (Array("cars", CAR_STATUS_CAR, CAR_SLOTS),)

CAR_DAMAGE_LAYOUT: tuple[Item, ...] = (Array("cars", CAR_DAMAGE_CAR, CAR_SLOTS),)

MOTION_EX_LAYOUT: tuple[Item, ...] = (
    Field("suspension_position", "f", 4, corners=True),
    Field("suspension_velocity", "f", 4, corners=True),
    Field("suspension_acceleration", "f", 4, corners=True),
    Field("wheel_speed", "f", 4, corners=True),
    Field("wheel_slip_ratio", "f", 4, corners=True),
    Field("wheel_slip_angle", "f", 4, corners=True),
    Field("wheel_lat_force", "f", 4, corners=True),
    Field("wheel_long_force", "f", 4, corners=True),
    Field("height_of_cog_above_ground", "f"),
    Field("local_velocity", "f", 3),
    Field("angular_velocity", "f", 3),
    Field("angular_acceleration", "f", 3),
    Field("front_wheels_angle", "f"),
    Field("wheel_vert_force", "f", 4, corners=True),
    Field("front_aero_height", "f"),
    Field("rear_aero_height", "f"),
    Field("front_roll_angle", "f"),
    Field("rear_roll_angle", "f"),
    Field("chassis_yaw", "f"),
    Field("chassis_pitch", "f"),
    Field("wheel_camber", "f", 4, corners=True),
    Field("wheel_camber_gain", "f", 4, corners=True),
)
