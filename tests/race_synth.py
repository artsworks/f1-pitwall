"""Deterministic synthetic race stream (docs/18 "Fixtures and tests").

Four cars on a 5 km track (track 7): rival 3 leads, rival 1 is P2 ahead of
the player (P3), rival 2 is P4 behind. One frame per second of session time
with Session, Lap Data, Car Status, Car Damage, Car Telemetry, Session
History and Event packets. Flagged synthetic per docs/07."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from pitwall.protocol.header import PacketId

from .synth import pack_packet

TRACK_M = 5000
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
    extra: dict[str, object] = field(default_factory=dict)


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
    speed = TRACK_M / (spec.base_ms / 1000.0)
    return {
        "current_lap_num": lap,
        "car_position": pos,
        "lap_distance": (d + offset_s * speed) % TRACK_M,
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

    def emit(pid: int, data: dict[str, object]) -> None:
        pkts.append((t, pack_packet(pid, data, session_time=t)))

    def event(code: bytes, detail: bytes = b"") -> None:
        emit(PacketId.EVENT, {"event_string_code": code, "event_data": detail.ljust(12, b"\0")})

    emit(
        PacketId.PARTICIPANTS,
        {"num_active_cars": 4, "cars": {i: {"name": n} for i, n in NAMES.items()}},
    )
    event(b"LGOT")
    history: dict[int, list[int]] = {1: [], 2: [], 3: []}
    stint_start = 1
    player_wear0 = 0.0
    player_last_ms = 0
    for lap in range(1, spec.laps + 1):
        if spec.player_pit_lap is not None and lap == spec.player_pit_lap + 1:
            stint_start = lap
            player_wear0 = 0.0
        age = lap - stint_start
        lap_ms = spec.base_ms + spec.deg_ms * age
        sc = 1 if spec.sc_laps is not None and spec.sc_laps[0] <= lap <= spec.sc_laps[1] else 0
        if sc:
            lap_ms = int(lap_ms * 1.4)
        frames = max(1, int(lap_ms / 1000 / spec.dt))
        wear = player_wear0 + spec.wear_pct_per_lap * age
        fuel = spec.fuel_kg - spec.fuel_kg_per_lap * (lap - 1)
        for f in range(frames):
            frac = f / frames
            d = frac * TRACK_M
            player_pitting = spec.player_pit_lap == lap and frac > 0.85
            rival_pitting = spec.rival_pit_lap == lap and frac > 0.85
            if f % 5 == 0:
                forecast: dict[str, object] = {}
                if spec.rain_lap is not None and lap >= spec.rain_lap:
                    forecast = {
                        "num_weather_forecast_samples": 3,
                        "weather_forecast_samples": {
                            0: {"session_type": 15, "time_offset": 0, "rain_percentage": 10},
                            1: {"session_type": 15, "time_offset": 10, "rain_percentage": 70},
                            2: {"session_type": 15, "time_offset": 30, "rain_percentage": 80},
                        },
                    }
                emit(
                    PacketId.SESSION,
                    {
                        "session_type": 15,
                        "track_id": spec.track_id,
                        "total_laps": spec.laps,
                        "track_length": TRACK_M,
                        "safety_car_status": sc,
                        **forecast,
                    },
                )

            emit(
                PacketId.LAP_DATA,
                {
                    "cars": {
                        0: {
                            "current_lap_num": lap,
                            "last_lap_time_ms": player_last_ms,
                            "car_position": 3,
                            "lap_distance": d,
                            "sector": int(frac * 3),
                            "result_status": 2,
                            "driver_status": 3 if spec.player_pit_lap == lap - 1 else 4,
                            "pit_status": 1 if player_pitting else 0,
                            "pit_lane_time_in_lane_ms": 19_500 if player_pitting else 0,
                            "delta_to_car_in_front_ms_part": int(spec.gap_ahead_s * 1000),
                        },
                        1: _rival(spec, lap, d, frac, 1, spec.gap_ahead_s, 2, rival_pitting),
                        2: _rival(spec, lap, d, frac, 2, -spec.gap_behind_s, 4, False),
                        3: _rival(spec, lap, d, frac, 3, 20.0, 1, False),
                    }
                },
            )
            emit(
                PacketId.CAR_STATUS,
                {
                    "cars": {
                        0: {
                            "fuel_in_tank": fuel - spec.fuel_kg_per_lap * frac,
                            "fuel_remaining_laps": (fuel - spec.fuel_kg_per_lap * frac)
                            / spec.fuel_kg_per_lap
                            - (spec.laps - lap + 1 - frac),
                            "actual_tyre_compound": 17,
                            "visual_tyre_compound": 17,
                            "tyres_age_laps": age,
                            "vehicle_fia_flags": 4 if spec.blue_flag_lap == lap and f < 5 else 0,
                            "ers_store_energy": 3_000_000.0,
                        }
                    }
                },
            )
            emit(PacketId.CAR_DAMAGE, {"cars": {0: {"tyres_wear": (wear,) * 4}}})
            hot = spec.hot_tyres_lap is not None and lap >= spec.hot_tyres_lap
            emit(
                PacketId.CAR_TELEMETRY,
                {
                    "cars": {
                        0: {
                            "tyres_inner_temperature": ((125 if hot else 95),) * 4,
                            "speed": 300 if (frac * 3) % 1 < 0.6 else 150,
                            "throttle": 1.0 if (frac * 3) % 1 < 0.6 else 0.4,
                            "steer": 0.0 if (frac * 3) % 1 < 0.6 else 0.3,
                        }
                    }
                },
            )
            if f % int(2 / spec.dt) == 0:
                for i in (1, 2, 3):
                    if history[i]:
                        emit(PacketId.SESSION_HISTORY, _history(i, history[i][-100:]))
            if spec.penalty_lap == lap and f == frames // 2:
                event(b"PENA", struct.pack("<BBBBBBB", 0, 7, 0, 255, 5, lap, 0))
            t += spec.dt
        player_last_ms = lap_ms
        for i in (1, 2, 3):
            pit = spec.rival_pit_lap if i == 1 else None
            rival_age = lap - (pit + 1) if pit is not None and lap > pit else lap - 1
            history[i].append(spec.base_ms + 50 + spec.deg_ms * rival_age)
    return pkts
