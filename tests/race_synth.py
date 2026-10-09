"""Deterministic synthetic race stream (docs/18 "Fixtures and tests").

Four cars on a 5 km track (track 7): rival 3 leads, rival 1 is P2 ahead of
the player (P3), rival 2 is P4 behind. One frame per second of session time
with Session, Lap Data, Car Status, Car Damage, Car Telemetry, Session
History and Event packets. Flagged synthetic per docs/07."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from random import Random

from pitwall.protocol.header import PacketId

from .synth import pack_packet

TRACK_M = 5000
ACTIVE_AERO_LOOKAHEAD_M = 250
OVERTAKE_LOOKAHEAD_M = 300
NAMES = {0: b"PLAYER", 1: b"NORRIS", 2: b"LECLERC", 3: b"VERSTAPPEN"}


@dataclass
class RaceSpec:
    laps: int = 12
    base_ms: int = 90_000
    deg_ms: int = 120
    wear_pct_per_lap: float = 3.0
    fuel_kg: float = 30.0
    fuel_kg_per_lap: float = 1.7
    player_pit_lap: int | None = None
    rival_pit_lap: int | None = None
    sc_laps: tuple[int, int] | None = None
    penalty_lap: int | None = None
    blue_flag_lap: int | None = None
    rain_lap: int | None = None
    gap_ahead_s: float = 1.5
    gap_behind_s: float = 3.0
    track_id: int = 7
    dt: float = 0.25
    hot_tyres_lap: int | None = None
    grid_position: int = 3
    wing_lost_lap: int | None = None  # front-right wing 100% from mid-lap
    puncture_lap: int | None = None  # rear-left tyre damage 100% from mid-lap
    vsc: bool = False  # sc_laps are a VSC instead of a full SC
    finish: bool = False  # chequered flag, then the line
    wear_scale: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)  # RL RR FL FR
    extra: dict[str, object] = field(default_factory=dict)
    fuel_ms_per_kg: float = 0.0
    session_type: int = 15
    weekend_link: int = 0
    session_uid: int = 0xDEADBEEF
    compound: int = 17
    tyre_inner_profile: tuple[float, ...] | None = None
    tyre_surface_profile: tuple[float, ...] | None = None
    thermal_window_c: tuple[float, float] | None = None
    thermal_penalty_ms: int = 0
    ers_deployed_j_per_lap: float = 0.0
    send_session_end: bool = False
    pit_lane_loss_ms: int = 0
    lap_noise_ms: float = 0.0
    seed: int = 0
    compound_after_stop: int | None = None
    deg_ms_after_stop: int | None = None
    pit_request_laps_early: int = 0
    pit_box_after_line: bool = False
    ct2: bool = False
    aero_zones_m: tuple[tuple[float, float], ...] = ()
    overtake_detect_m: float | None = None
    overtake_activation_offset_m: float = 80.0
    overtake_gap_s: float = 1.0
    rival_overtake_cars: tuple[int, ...] = ()


def _rival_lap_distance(spec: RaceSpec, d: float, offset_s: float) -> float:
    speed = TRACK_M / (spec.base_ms / 1000.0)
    return (d + offset_s * speed) % TRACK_M


def _inside_aero_zone(distance_m: float, zone: tuple[float, float]) -> bool:
    start_m, end_m = (point % TRACK_M for point in zone)
    distance_m %= TRACK_M
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= distance_m < end_m
    return distance_m >= start_m or distance_m < end_m


class _Ct2Model:
    def __init__(self, spec: RaceSpec) -> None:
        self.spec = spec
        self._zone_states: dict[int, tuple[bool, ...]] = {}
        self._player_distance_m: float | None = None
        self._overtake_available = 0
        self._overtake_active = 0

    def fields_for_car(
        self,
        car_idx: int,
        lap_distance_m: float,
        gap_ahead_s: float,
    ) -> dict[str, int]:
        distance_m = lap_distance_m % TRACK_M
        inside_zones = tuple(_inside_aero_zone(distance_m, zone) for zone in self.spec.aero_zones_m)
        previous_zones = self._zone_states.get(car_idx, (False,) * len(inside_zones))
        active_aero_available = int(
            any(
                inside and not was_inside
                for inside, was_inside in zip(inside_zones, previous_zones, strict=True)
            )
        )
        self._zone_states[car_idx] = inside_zones
        active_aero_mode = int(any(inside_zones))

        active_aero_activation_distance = 0
        if not active_aero_mode:
            upcoming_starts = (
                (start_m % TRACK_M - distance_m) % TRACK_M
                for start_m, _end_m in self.spec.aero_zones_m
                if (start_m % TRACK_M) != (_end_m % TRACK_M)
            )
            in_range = (
                activation_distance
                for activation_distance in upcoming_starts
                if 0 < activation_distance <= ACTIVE_AERO_LOOKAHEAD_M
            )
            active_aero_activation_distance = int(min(in_range, default=0))

        if car_idx == 0:
            self._update_overtake(distance_m, gap_ahead_s)
            overtake_available = self._overtake_available
            overtake_active = self._overtake_active
            overtake_activation_distance = self._overtake_distance(distance_m)
        else:
            overtake_available = int(car_idx in self.spec.rival_overtake_cars)
            overtake_active = overtake_available
            overtake_activation_distance = 0

        return {
            "active_aero_mode": active_aero_mode,
            "active_aero_available": active_aero_available,
            "active_aero_activation_distance": active_aero_activation_distance,
            "overtake_available": overtake_available,
            "overtake_active": overtake_active,
            "overtake_activation_distance": overtake_activation_distance,
            "regulations_2026": 1,
        }

    def _overtake_distance(self, distance_m: float) -> int:
        if self.spec.overtake_detect_m is None:
            return 0
        activation_m = (
            self.spec.overtake_detect_m + self.spec.overtake_activation_offset_m
        ) % TRACK_M
        activation_distance = (activation_m - distance_m) % TRACK_M
        if 0 < activation_distance <= OVERTAKE_LOOKAHEAD_M:
            return int(activation_distance)
        return 0

    def _update_overtake(self, distance_m: float, gap_ahead_s: float) -> None:
        if self._player_distance_m is not None:
            previous = self._player_distance_m
            if self.spec.overtake_detect_m is not None:
                detect_m = self.spec.overtake_detect_m % TRACK_M
                if _crossed(previous, distance_m, detect_m):
                    self._overtake_available = int(gap_ahead_s <= self.spec.overtake_gap_s)
            if self.spec.overtake_detect_m is not None:
                activation_m = (
                    self.spec.overtake_detect_m + self.spec.overtake_activation_offset_m
                ) % TRACK_M
                if _crossed(previous, distance_m, activation_m):
                    self._overtake_active = self._overtake_available
        self._player_distance_m = distance_m


def _crossed(previous_m: float, current_m: float, point_m: float) -> bool:
    if current_m >= previous_m:
        return previous_m < point_m <= current_m
    return point_m > previous_m or point_m <= current_m


def _rival(
    spec: RaceSpec,
    lap: int,
    d: float,
    frac: float,
    i: int,
    offset_s: float,
    pos: int,
    pitting: bool,
) -> dict[str, object]:
    return {
        "current_lap_num": lap,
        "car_position": pos,
        "lap_distance": _rival_lap_distance(spec, d, offset_s),
        "result_status": 2,
        "driver_status": 4,
        "pit_status": 1 if pitting else 0,
        "delta_to_car_in_front_ms_part": int(spec.gap_behind_s * 1000) if i == 2 else 500,
        "sector": int(frac * 3),
    }


def _history(i: int, laps: list[int]) -> dict[str, object]:
    return {
        "car_idx": i,
        "num_laps": len(laps),
        "num_tyre_stints": 1,
        "laps": {n: {"lap_time_ms": ms, "lap_valid_bit_flags": 0x01} for n, ms in enumerate(laps)},
        "tyre_stints": {
            0: {"end_lap": 255, "tyre_actual_compound": 17, "tyre_visual_compound": 17}
        },
    }


def race_stream(spec: RaceSpec) -> list[tuple[float, bytes]]:
    pkts: list[tuple[float, bytes]] = []
    t = 0.0
    frame = 1
    ct2_model = _Ct2Model(spec) if spec.ct2 else None

    def emit(pid: int, data: dict[str, object]) -> None:
        pkts.append(
            (
                t,
                pack_packet(
                    pid,
                    data,
                    session_uid=spec.session_uid,
                    session_time=t,
                    frame=frame,
                ),
            )
        )

    def event(code: bytes, detail: bytes = b"") -> None:
        emit(PacketId.EVENT, {"event_string_code": code, "event_data": detail.ljust(12, b"\0")})

    def emit_ct2(lap_distances: dict[int, float], gap_ahead_ms: int) -> None:
        if ct2_model is None:
            return
        gap_ahead_s = gap_ahead_ms / 1000.0
        emit(
            PacketId.CAR_TELEMETRY_2,
            {
                "cars": {
                    car_idx: ct2_model.fields_for_car(car_idx, distance_m, gap_ahead_s)
                    for car_idx, distance_m in lap_distances.items()
                }
            },
        )

    emit(
        PacketId.PARTICIPANTS,
        {"num_active_cars": 4, "cars": {i: {"name": n} for i, n in NAMES.items()}},
    )
    event(b"LGOT")
    history: dict[int, list[int]] = {1: [], 2: [], 3: []}
    stint_start = 1
    player_wear0 = 0.0
    player_last_ms = 0
    rng = Random(spec.seed)
    for lap in range(1, spec.laps + 1):
        if spec.player_pit_lap is not None and lap == spec.player_pit_lap + 1:
            stint_start = lap
            player_wear0 = 0.0
        age = lap - stint_start
        after_stop = spec.player_pit_lap is not None and lap > spec.player_pit_lap
        deg_ms = (
            spec.deg_ms_after_stop
            if after_stop and spec.deg_ms_after_stop is not None
            else spec.deg_ms
        )
        fuel = spec.fuel_kg - spec.fuel_kg_per_lap * (lap - 1)
        lap_ms = int(spec.base_ms + deg_ms * age + spec.fuel_ms_per_kg * fuel)
        if spec.tyre_inner_profile is not None:
            tyre_inner = spec.tyre_inner_profile[min(lap - 1, len(spec.tyre_inner_profile) - 1)]
        else:
            tyre_inner = 125 if spec.hot_tyres_lap is not None and lap >= spec.hot_tyres_lap else 95
        if (
            spec.thermal_window_c is not None
            and not spec.thermal_window_c[0] <= tyre_inner <= spec.thermal_window_c[1]
        ):
            lap_ms += spec.thermal_penalty_ms
        sc = 0
        if spec.sc_laps is not None and spec.sc_laps[0] <= lap <= spec.sc_laps[1]:
            sc = 2 if spec.vsc else 1
        if sc:
            lap_ms = int(lap_ms * 1.4)
        if spec.pit_lane_loss_ms > 0 and spec.player_pit_lap is not None:
            in_lap_loss_ms = int(spec.pit_lane_loss_ms * 0.4)
            if lap == spec.player_pit_lap:
                lap_ms += in_lap_loss_ms
            elif lap == spec.player_pit_lap + 1:
                lap_ms += spec.pit_lane_loss_ms - in_lap_loss_ms
        noise_ms = rng.gauss(0.0, spec.lap_noise_ms) if spec.lap_noise_ms > 0 else 0.0
        if not sc:
            lap_ms += int(noise_ms)
        frames = max(1, int(lap_ms / 1000 / spec.dt))
        wear = player_wear0 + spec.wear_pct_per_lap * age
        for f in range(frames):
            frame += 1
            frac = f / frames
            d = frac * TRACK_M
            if spec.pit_box_after_line and spec.player_pit_lap == lap:
                player_pit_status = 1 if frac > 0.85 else 0
            elif spec.pit_box_after_line and spec.player_pit_lap == lap - 1:
                if frac < 0.02:
                    player_pit_status = 1
                elif frac < 0.05:
                    player_pit_status = 2
                elif frac < 0.15:
                    player_pit_status = 1
                else:
                    player_pit_status = 0
            else:
                player_pit_status = int(spec.player_pit_lap == lap and frac > 0.85)
            player_pitting = player_pit_status != 0
            rival_pitting = spec.rival_pit_lap == lap and frac > 0.85
            player_driver_status = 3 if spec.player_pit_lap == lap - 1 else 4
            if spec.pit_lane_loss_ms > 0 and spec.player_pit_lap == lap - 1:
                player_driver_status = 2
            if spec.player_pit_lap is not None:
                request_lap = max(1, spec.player_pit_lap - spec.pit_request_laps_early)
                stop_lap = spec.player_pit_lap + int(spec.pit_box_after_line)
                stop_frac = 0.02 if spec.pit_box_after_line else 0.95
                if request_lap <= lap < stop_lap or (lap == stop_lap and frac < stop_frac):
                    player_driver_status = 2
                elif lap == stop_lap:
                    player_driver_status = 3
                elif lap == stop_lap + 1:
                    player_driver_status = 3 if not spec.pit_box_after_line or f == 0 else 4
            if f % 5 == 0:
                forecast: dict[str, object] = {}
                if spec.rain_lap is not None and lap >= spec.rain_lap:
                    forecast = {
                        "num_weather_forecast_samples": 3,
                        "weather_forecast_samples": {
                            0: {
                                "session_type": spec.session_type,
                                "time_offset": 0,
                                "rain_percentage": 10,
                            },
                            1: {
                                "session_type": spec.session_type,
                                "time_offset": 10,
                                "rain_percentage": 70,
                            },
                            2: {
                                "session_type": spec.session_type,
                                "time_offset": 30,
                                "rain_percentage": 80,
                            },
                        },
                    }
                emit(
                    PacketId.SESSION,
                    {
                        "session_type": spec.session_type,
                        "weekend_link_identifier": spec.weekend_link,
                        "track_id": spec.track_id,
                        "total_laps": spec.laps,
                        "track_length": TRACK_M,
                        "safety_car_status": sc,
                        **forecast,
                    },
                )

            player_lap_data: dict[str, object] = {
                "current_lap_num": lap,
                "last_lap_time_ms": player_last_ms,
                "car_position": 3,
                "grid_position": spec.grid_position,
                "lap_distance": d,
                "sector": int(frac * 3),
                "result_status": 2,
                "driver_status": player_driver_status,
                "pit_status": player_pit_status,
                "pit_lane_time_in_lane_ms": 19_500 if player_pitting else 0,
                "delta_to_car_in_front_ms_part": int(spec.gap_ahead_s * 1000),
            }
            rival_lap_data = {
                1: _rival(spec, lap, d, frac, 1, spec.gap_ahead_s, 2, rival_pitting),
                2: _rival(spec, lap, d, frac, 2, -spec.gap_behind_s, 4, False),
                3: _rival(spec, lap, d, frac, 3, 20.0, 1, False),
            }
            emit(PacketId.LAP_DATA, {"cars": {0: player_lap_data, **rival_lap_data}})
            emit_ct2(
                {
                    0: d,
                    1: _rival_lap_distance(spec, d, spec.gap_ahead_s),
                    2: _rival_lap_distance(spec, d, -spec.gap_behind_s),
                    3: _rival_lap_distance(spec, d, 20.0),
                },
                int(player_lap_data["delta_to_car_in_front_ms_part"]),
            )
            player_status: dict[str, object] = {
                "fuel_in_tank": fuel - spec.fuel_kg_per_lap * frac,
                "fuel_remaining_laps": (fuel - spec.fuel_kg_per_lap * frac) / spec.fuel_kg_per_lap
                - (spec.laps - lap + 1 - frac),
                "actual_tyre_compound": (
                    spec.compound_after_stop
                    if after_stop and spec.compound_after_stop is not None
                    else spec.compound
                ),
                "visual_tyre_compound": (
                    spec.compound_after_stop
                    if after_stop and spec.compound_after_stop is not None
                    else spec.compound
                ),
                "tyres_age_laps": age,
                "vehicle_fia_flags": 4 if spec.blue_flag_lap == lap and f < 5 else 0,
                "ers_store_energy": 3_000_000.0,
            }
            if spec.ers_deployed_j_per_lap > 0:
                player_status["ers_deployed_this_lap"] = spec.ers_deployed_j_per_lap
            emit(PacketId.CAR_STATUS, {"cars": {0: player_status}})
            damage: dict[str, object] = {
                "tyres_wear": tuple(wear * k for k in spec.wear_scale),
                "tyres_damage": tuple(int(wear * k) for k in spec.wear_scale),
            }
            if spec.wing_lost_lap is not None and (lap, frac) >= (spec.wing_lost_lap, 0.5):
                damage["front_right_wing_damage"] = 100
            if spec.puncture_lap is not None and (lap, frac) >= (spec.puncture_lap, 0.5):
                damage["tyres_damage"] = (100, int(wear), int(wear), int(wear))
            emit(PacketId.CAR_DAMAGE, {"cars": {0: damage}})
            hot = spec.hot_tyres_lap is not None and lap >= spec.hot_tyres_lap
            inner = tyre_inner if spec.tyre_inner_profile is not None else (125 if hot else 95)
            surface = (
                spec.tyre_surface_profile[min(lap - 1, len(spec.tyre_surface_profile) - 1)]
                if spec.tyre_surface_profile is not None
                else inner
            )
            telemetry_car: dict[str, object] = {
                "tyres_inner_temperature": (int(inner),) * 4,
                "speed": 300 if (frac * 3) % 1 < 0.6 else 150,
                "throttle": 1.0 if (frac * 3) % 1 < 0.6 else 0.4,
                "steer": 0.0 if (frac * 3) % 1 < 0.6 else 0.3,
            }
            if spec.tyre_surface_profile is not None:
                telemetry_car["tyres_surface_temperature"] = (int(surface),) * 4
            emit(PacketId.CAR_TELEMETRY, {"cars": {0: telemetry_car}})
            if f % int(2 / spec.dt) == 0:
                for i in (1, 2, 3):
                    if history[i]:
                        emit(PacketId.SESSION_HISTORY, _history(i, history[i][-100:]))
            if spec.sc_laps is not None and lap == spec.sc_laps[1] and f == frames // 2:
                event(b"SCAR", struct.pack("<BB", 2 if spec.vsc else 1, 1))  # returning
            if spec.penalty_lap == lap and f == frames // 2:
                event(b"PENA", struct.pack("<BBBBBBB", 4, 7, 0, 255, 5, lap, 0))  # time penalty
            t += spec.dt
        player_last_ms = lap_ms
        for i in (1, 2, 3):
            pit = spec.rival_pit_lap if i == 1 else None
            rival_age = lap - (pit + 1) if pit is not None and lap > pit else lap - 1
            history[i].append(spec.base_ms + 50 + spec.deg_ms * rival_age)
    if spec.finish:
        event(b"CHQF")
        for f in range(int(4 / spec.dt)):
            frame += 1
            d = f * spec.dt * TRACK_M / (spec.base_ms / 1000.0)
            player_lap_data = {
                "current_lap_num": spec.laps + 1,
                "last_lap_time_ms": player_last_ms,
                "car_position": 3,
                "grid_position": spec.grid_position,
                "lap_distance": d,
                "result_status": 3,
                "driver_status": 4,
                "delta_to_car_in_front_ms_part": int(spec.gap_ahead_s * 1000),
            }
            rival_lap_data = {
                1: _rival(spec, spec.laps + 1, d, 0.0, 1, spec.gap_ahead_s, 2, False),
                2: _rival(spec, spec.laps + 1, d, 0.0, 2, -spec.gap_behind_s, 4, False),
                3: _rival(spec, spec.laps + 1, d, 0.0, 3, 20.0, 1, False),
            }
            emit(
                PacketId.LAP_DATA,
                {
                    "cars": {
                        0: player_lap_data,
                        **rival_lap_data,
                    }
                },
            )
            emit_ct2(
                {
                    0: d,
                    1: _rival_lap_distance(spec, d, spec.gap_ahead_s),
                    2: _rival_lap_distance(spec, d, -spec.gap_behind_s),
                    3: _rival_lap_distance(spec, d, 20.0),
                },
                int(player_lap_data["delta_to_car_in_front_ms_part"]),
            )
            t += spec.dt
    if spec.send_session_end:
        event(b"SEND")
    return pkts
