"""Seeded full-field synthetic race recordings."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np

from pitwall.derive import synthetic_uid
from pitwall.model.deg import DEG_FUEL_REF, fuel_adjusted_deg, scoped
from pitwall.net.recording import RecordingWriter, compress_recording
from pitwall.protocol.header import PacketId
from pitwall.protocol.pack import pack_packet

_DEFAULT_DEG = {16: 110.0, 17: 70.0, 18: 45.0}
_DRIVER_NAMES = ("PLAYER", *(f"DRIVER {number:02}" for number in range(2, 25)))
_TEAM_IDS = (0, 1, 3, 0, 2, 4, 5, 5, 6, 7, 7, 8, 9, 6, 10, 10, 2, 9, 11, 11, 3, 4, 8, 1)
_GRID_SLOT_STAGGER_S = 0.3


@dataclass(frozen=True)
class Priors:
    base_ms: float = 90_000.0
    deg_ms_per_lap: dict[int, float] = field(default_factory=lambda: dict(_DEFAULT_DEG))
    fuel_kg_per_lap: float = 1.7
    fuel_ms_per_kg: float = 30.0
    start_fuel_kg: float = 90.0
    pit_loss_green_ms: float = 22_000.0
    pit_loss_sc_ms: float = 8_000.0
    pit_loss_vsc_ms: float = 12_000.0


@dataclass(frozen=True)
class FieldSpec:
    track_id: int
    laps: int
    cars: int = 20
    seed: int = 1
    priors: Priors = field(default_factory=Priors)
    track_length_m: int = 5_000
    dt: float = 0.25
    pace_spread_ms: float = 1_200.0
    lap_noise_ms: float = 150.0
    grid_position: int | None = None
    sc_laps: tuple[int, int] | None = None
    vsc: bool = False
    player_stops: tuple[tuple[int, int], ...] = ()
    start_compound: int = 17
    rival_two_stop_prob: float = 0.3
    rival_sc_stop_prob: float = 0.6
    session_type: int = 15
    finish: bool = True
    send_session_end: bool = True

    @property
    def player_grid_position(self) -> int:
        return self.grid_position if self.grid_position is not None else (self.cars + 1) // 2

    def __post_init__(self) -> None:
        if self.track_id < 0 or self.track_id > 127:
            raise ValueError("track_id must fit the protocol signed byte")
        if self.laps < 1 or self.laps > 255:
            raise ValueError("laps must be between 1 and 255")
        if self.cars < 1 or self.cars > len(_DRIVER_NAMES):
            raise ValueError(f"cars must be between 1 and {len(_DRIVER_NAMES)}")
        if self.track_length_m < 1 or self.track_length_m > 65_535:
            raise ValueError("track_length_m must be between 1 and 65535")
        if not math.isfinite(self.dt) or self.dt <= 0:
            raise ValueError("dt must be a positive finite number")
        if not math.isfinite(self.pace_spread_ms) or self.pace_spread_ms < 0:
            raise ValueError("pace_spread_ms must be finite and non-negative")
        if not math.isfinite(self.lap_noise_ms) or self.lap_noise_ms < 0:
            raise ValueError("lap_noise_ms must be finite and non-negative")
        grid_position = self.grid_position
        if grid_position is None:
            grid_position = (self.cars + 1) // 2
            object.__setattr__(self, "grid_position", grid_position)
        elif not 1 <= grid_position <= self.cars:
            raise ValueError("grid_position must be between 1 and cars")
        if not 0 <= self.rival_two_stop_prob <= 1 or not 0 <= self.rival_sc_stop_prob <= 1:
            raise ValueError("rival strategy probabilities must be between 0 and 1")
        if self.sc_laps is not None:
            start, end = self.sc_laps
            if start < 1 or end < start or end > self.laps:
                raise ValueError("sc_laps must be an ascending window within the race")
        for lap, compound in self.player_stops:
            if lap < 1 or lap >= self.laps:
                raise ValueError("player stop laps must be between 1 and laps - 1")
            if not 0 <= compound <= 255:
                raise ValueError("tyre compounds must fit an unsigned byte")


@dataclass(frozen=True)
class GeneratedRace:
    path: Path
    session_uid: int
    sha256: str
    spec_hash: str
    seed: int


def _canonical_spec(spec: FieldSpec) -> tuple[str, str]:
    body = json.dumps(asdict(spec), sort_keys=True, separators=(",", ":"))
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


def _prior_value(
    rows: dict[tuple[int, str], float],
    compound: int,
    name: str,
    race_laps: int,
) -> float | None:
    value = rows.get((compound, scoped(name, race_laps)))
    if value is None:
        value = rows.get((compound, name))
    return value


def _priors_from_values(
    values: dict[tuple[int, str], float], race_laps: int
) -> tuple[Priors, list[str]]:
    defaults = Priors()
    defaulted: list[str] = []

    def get(compound: int, name: str, default: float, label: str) -> float:
        value = _prior_value(values, compound, name, race_laps)
        if value is None or not math.isfinite(value):
            defaulted.append(label)
            return default
        return float(value)

    compounds = set(defaults.deg_ms_per_lap)
    compounds.update(compound for compound, name in values if name in {"base_ms", "deg_ms_per_lap"})
    base_values: dict[int, float] = {}
    degrees: dict[int, float] = {}
    for compound in sorted(compounds):
        base_values[compound] = get(compound, "base_ms", defaults.base_ms, f"base_ms[{compound}]")
        degree = get(
            compound,
            "deg_ms_per_lap",
            defaults.deg_ms_per_lap.get(compound, 80.0),
            f"deg_ms_per_lap[{compound}]",
        )
        ref = _prior_value(values, compound, DEG_FUEL_REF, race_laps)
        fuel_lap = _prior_value(values, compound, "fuel_ms_per_lap", race_laps)
        if ref is not None:
            degree = fuel_adjusted_deg(degree, ref, fuel_lap if fuel_lap is not None else ref)
        degrees[compound] = degree

    fuel_burn = get(0, "fuel_kg_per_lap", defaults.fuel_kg_per_lap, "fuel_kg_per_lap")
    fuel_lap = _prior_value(values, 0, "fuel_ms_per_lap", race_laps)
    if fuel_lap is None:
        fuel_lap = next(
            (
                value
                for compound in sorted(compounds)
                if (value := _prior_value(values, compound, "fuel_ms_per_lap", race_laps))
                is not None
            ),
            None,
        )
    if fuel_lap is None:
        defaulted.append("fuel_ms_per_kg")
        fuel_per_kg = defaults.fuel_ms_per_kg
    elif fuel_burn <= 0:
        defaulted.append("fuel_ms_per_kg")
        fuel_per_kg = defaults.fuel_ms_per_kg
    else:
        fuel_per_kg = fuel_lap / fuel_burn
    start_fuel = get(0, "start_fuel_kg", defaults.start_fuel_kg, "start_fuel_kg")
    pit_green = get(0, "pit_loss_green_ms", defaults.pit_loss_green_ms, "pit_loss_green_ms")
    pit_sc = get(0, "pit_loss_sc_ms", defaults.pit_loss_sc_ms, "pit_loss_sc_ms")
    pit_vsc = get(0, "pit_loss_vsc_ms", defaults.pit_loss_vsc_ms, "pit_loss_vsc_ms")
    # Live folds store full-fuel stint-start pace under the same name. They are about 2.8 s apart.
    base = base_values.get(17, base_values.get(min(base_values, default=17), defaults.base_ms))
    return (
        Priors(
            base_ms=base,
            deg_ms_per_lap=degrees,
            fuel_kg_per_lap=fuel_burn,
            fuel_ms_per_kg=fuel_per_kg,
            start_fuel_kg=start_fuel,
            pit_loss_green_ms=pit_green,
            pit_loss_sc_ms=pit_sc,
            pit_loss_vsc_ms=pit_vsc,
        ),
        defaulted,
    )


def load_priors(db_path: Path | str, track_id: int, race_laps: int) -> tuple[Priors, list[str]]:
    """Read track priors from an existing SQLite database without changing it."""
    path = Path(db_path).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT compound, name, value FROM model_params WHERE track_id=?", (track_id,)
        ).fetchall()
    values = {(int(compound), str(name)): float(value) for compound, name, value in rows}
    return _priors_from_values(values, race_laps)


def load_priors_json(path: Path | str) -> tuple[Priors, list[str]]:
    """Read prior values from JSON with the same fields used by ``load_priors``."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("priors JSON must contain an object")
    body = data.get("priors", data)
    if not isinstance(body, dict):
        raise ValueError("priors JSON field 'priors' must contain an object")
    values: dict[tuple[int, str], float] = {}
    for compound, params in body.get("compounds", {}).items():
        if not isinstance(params, dict):
            continue
        for name, value in params.items():
            values[(int(compound), str(name))] = float(value)
    for name in (
        "fuel_kg_per_lap",
        "start_fuel_kg",
        "pit_loss_green_ms",
        "pit_loss_sc_ms",
        "pit_loss_vsc_ms",
    ):
        if name in body:
            values[(0, name)] = float(body[name])
    if "fuel_ms_per_kg" in body:
        values[(0, "fuel_ms_per_lap")] = float(body["fuel_ms_per_kg"]) * float(
            body.get("fuel_kg_per_lap", Priors().fuel_kg_per_lap)
        )
    priors, defaulted = _priors_from_values(values, int(data.get("race_laps", 0)))
    direct = body.get("deg_ms_per_lap")
    if isinstance(direct, dict):
        priors = Priors(
            base_ms=float(body.get("base_ms", priors.base_ms)),
            deg_ms_per_lap={int(k): float(v) for k, v in direct.items()},
            fuel_kg_per_lap=priors.fuel_kg_per_lap,
            fuel_ms_per_kg=float(body.get("fuel_ms_per_kg", priors.fuel_ms_per_kg)),
            start_fuel_kg=priors.start_fuel_kg,
            pit_loss_green_ms=priors.pit_loss_green_ms,
            pit_loss_sc_ms=priors.pit_loss_sc_ms,
            pit_loss_vsc_ms=priors.pit_loss_vsc_ms,
        )
        defaulted = []
        for key in (
            "base_ms",
            "fuel_kg_per_lap",
            "fuel_ms_per_kg",
            "start_fuel_kg",
            "pit_loss_green_ms",
            "pit_loss_sc_ms",
            "pit_loss_vsc_ms",
        ):
            if key not in body:
                defaulted.append(key)
    elif "base_ms" in body:
        priors = Priors(
            base_ms=float(body["base_ms"]),
            deg_ms_per_lap=priors.deg_ms_per_lap,
            fuel_kg_per_lap=priors.fuel_kg_per_lap,
            fuel_ms_per_kg=priors.fuel_ms_per_kg,
            start_fuel_kg=priors.start_fuel_kg,
            pit_loss_green_ms=priors.pit_loss_green_ms,
            pit_loss_sc_ms=priors.pit_loss_sc_ms,
            pit_loss_vsc_ms=priors.pit_loss_vsc_ms,
        )
        defaulted = [key for key in defaulted if key != "base_ms"]
    return priors, defaulted


def _pit_loss(priors: Priors, sc_laps: tuple[int, int] | None, vsc: bool, lap: int) -> float:
    if sc_laps is not None and sc_laps[0] <= lap <= sc_laps[1]:
        return priors.pit_loss_vsc_ms if vsc else priors.pit_loss_sc_ms
    return priors.pit_loss_green_ms


def _strategy_stops(spec: FieldSpec, rng: np.random.Generator) -> list[list[tuple[int, int]]]:
    compounds = sorted(set(spec.priors.deg_ms_per_lap) | {16, 17, 18})
    alternate = [compound for compound in compounds if compound != spec.start_compound]
    if not alternate:
        alternate = [16 if spec.start_compound != 16 else 17]
    stops: list[list[tuple[int, int]]] = [list(spec.player_stops)]
    for _ in range(1, spec.cars):
        two_stops = spec.laps >= 8 and rng.random() < spec.rival_two_stop_prob
        first_lap = int(round(spec.laps * rng.uniform(0.35, 0.6)))
        first_lap = min(spec.laps - 1, max(1, first_lap))
        first_compound = int(rng.choice(alternate))
        race_stops = [(first_lap, first_compound)]
        if two_stops:
            second_lap = min(spec.laps - 1, max(first_lap + 2, int(round(spec.laps * 0.78))))
            second_compound = spec.start_compound
            if second_lap > first_lap:
                race_stops.append((second_lap, second_compound))
        if spec.sc_laps is not None:
            sc_lap = spec.sc_laps[0]
            if (
                3 <= sc_lap < spec.laps
                and rng.random() < spec.rival_sc_stop_prob
                and race_stops
                and race_stops[0][0] >= sc_lap
            ):
                race_stops[0] = (sc_lap, race_stops[0][1])
        stops.append(sorted(race_stops))
    for car_stops in stops:
        car_stops.sort()
    return stops


def _pace_offsets(spec: FieldSpec, rng: np.random.Generator) -> np.ndarray:
    offsets = rng.normal(0.0, spec.pace_spread_ms, spec.cars - 1).astype(np.float32)
    offsets.sort()
    slot = min(spec.player_grid_position - 1, len(offsets))
    if not len(offsets):
        player_offset = np.float32(0.0)
    elif slot == 0:
        player_offset = np.float32(offsets[0] - spec.pace_spread_ms * 0.1)
    elif slot == len(offsets):
        player_offset = np.float32(offsets[-1] + spec.pace_spread_ms * 0.1)
    else:
        player_offset = np.float32((offsets[slot - 1] + offsets[slot]) / 2)
    return np.concatenate((np.array([player_offset], dtype=np.float32), offsets))


def _grid_positions(spec: FieldSpec) -> list[int]:
    positions = [spec.player_grid_position]
    positions.extend(
        position for position in range(1, spec.cars + 1) if position != spec.player_grid_position
    )
    return positions


def _grid_start_offsets(spec: FieldSpec) -> list[float]:
    return [(position - 1) * _GRID_SLOT_STAGGER_S for position in _grid_positions(spec)]


def _build_lap_times(spec: FieldSpec) -> tuple[np.ndarray, list[list[tuple[int, int]]]]:
    rng = np.random.default_rng(spec.seed)
    offsets = _pace_offsets(spec, rng)
    stops = _strategy_stops(spec, rng)
    lap_times = np.zeros((spec.cars, spec.laps), dtype=np.float32)
    noise = rng.normal(0.0, spec.lap_noise_ms, (spec.cars, spec.laps)).astype(np.float32)
    start_offsets_ms = np.asarray(_grid_start_offsets(spec), dtype=np.float32) * 1000.0
    for car in range(spec.cars):
        current_stops = stops[car]
        for lap_idx in range(spec.laps):
            lap = lap_idx + 1
            completed_stops = [
                (stop_lap, compound) for stop_lap, compound in current_stops if stop_lap < lap
            ]
            compound = completed_stops[-1][1] if completed_stops else spec.start_compound
            last_stop = completed_stops[-1][0] if completed_stops else 0
            age = lap - last_stop - 1
            fuel = max(0.0, spec.priors.start_fuel_kg - spec.priors.fuel_kg_per_lap * lap_idx)
            deg = spec.priors.deg_ms_per_lap.get(compound, 80.0)
            value = (
                spec.priors.base_ms
                + float(offsets[car])
                + deg * age
                + spec.priors.fuel_ms_per_kg * fuel
                + float(noise[car, lap_idx])
            )
            stop_here = next((stop for stop in current_stops if stop[0] == lap), None)
            if stop_here is not None:
                loss = _pit_loss(spec.priors, spec.sc_laps, spec.vsc, lap)
                value += loss * 0.4
            if any(stop_lap + 1 == lap for stop_lap, _ in current_stops):
                prior_stop_lap = next(
                    stop_lap for stop_lap, _ in current_stops if stop_lap + 1 == lap
                )
                value += _pit_loss(spec.priors, spec.sc_laps, spec.vsc, prior_stop_lap) * 0.6
            if spec.sc_laps is not None and spec.sc_laps[0] <= lap <= spec.sc_laps[1]:
                value *= 1.3 if spec.vsc else 1.4
            lap_times[car, lap_idx] = max(1.0, value)
    if spec.sc_laps is not None and not spec.vsc:
        for lap_idx in range(spec.sc_laps[0] - 1, spec.sc_laps[1]):
            cumulative = (
                start_offsets_ms
                + np.cumsum(lap_times[:, : lap_idx + 1], axis=1, dtype=np.float32)[:, -1]
            )
            order = np.argsort(cumulative, kind="stable")
            target = np.minimum(
                cumulative[order],
                cumulative[order[0]] + np.arange(spec.cars, dtype=np.float32) * 800.0,
            )
            adjusted = np.empty_like(cumulative)
            adjusted[order] = target
            previous = (
                start_offsets_ms
                + np.cumsum(lap_times[:, :lap_idx], axis=1, dtype=np.float32)[:, -1]
                if lap_idx
                else start_offsets_ms
            )
            lap_times[:, lap_idx] = np.maximum(adjusted - previous, 1.0)
    return lap_times, stops


def _split_time(ms: float) -> tuple[int, int]:
    minutes, remainder = divmod(max(0, int(ms)), 60_000)
    return min(255, remainder), min(255, minutes)


def _session_history(
    car: int,
    completed_laps: int,
    lap_times: np.ndarray,
    stops: list[tuple[int, int]],
    start_compound: int,
    sc_laps: tuple[int, int] | None,
) -> dict[str, object]:
    lap_entries: dict[int, dict[str, int]] = {}
    for idx in range(min(completed_laps, lap_times.shape[1], 100)):
        sector = int(lap_times[car, idx] / 3)
        ms_part, minutes = _split_time(sector)
        lap_entries[idx] = {
            "lap_time_ms": int(lap_times[car, idx]),
            "sector1_ms_part": ms_part,
            "sector1_minutes": minutes,
            "sector2_ms_part": ms_part,
            "sector2_minutes": minutes,
            "sector3_ms_part": ms_part,
            "sector3_minutes": minutes,
            "lap_valid_bit_flags": 1,
        }
    completed_stops = [(lap, compound) for lap, compound in stops if lap <= completed_laps]
    compounds = [start_compound, *(compound for _, compound in completed_stops)]
    stints: dict[int, dict[str, int]] = {}
    for idx, compound in enumerate(compounds):
        end_lap = completed_stops[idx][0] if idx < len(completed_stops) else 255
        stints[idx] = {
            "end_lap": end_lap,
            "tyre_actual_compound": compound,
            "tyre_visual_compound": compound,
        }
    return {
        "car_idx": car,
        "num_laps": len(lap_entries),
        "num_tyre_stints": len(stints),
        "laps": lap_entries,
        "tyre_stints": stints,
    }


def _packet_time(
    packet_id: int,
    data: dict[str, object],
    *,
    uid: int,
    elapsed_s: float,
    frame: int,
) -> bytes:
    return pack_packet(packet_id, data, session_uid=uid, session_time=elapsed_s, frame=frame)


def _frame_packets(
    spec: FieldSpec,
    lap_times: np.ndarray,
    stops: list[list[tuple[int, int]]],
    uid: int,
    elapsed_s: float,
    frame: int,
    frame_index: int,
) -> list[bytes]:
    cumulative = np.cumsum(lap_times, axis=1, dtype=np.float32)
    grid_positions = _grid_positions(spec)
    start_offsets_s = _grid_start_offsets(spec)
    completed = [
        int(
            np.searchsorted(
                cumulative[car],
                max(0.0, elapsed_s - start_offsets_s[car]) * 1000,
                side="right",
            )
        )
        for car in range(spec.cars)
    ]
    lap_numbers = [min(spec.laps + 1, count + 1) for count in completed]
    fractions: list[float] = []
    distances: list[float] = []
    total_distances: list[float] = []
    for car, count in enumerate(completed):
        if count >= spec.laps:
            fraction = 0.0
            completed_dist = spec.laps
        else:
            prior = float(cumulative[car, count - 1]) if count else 0.0
            duration = float(lap_times[car, count])
            car_elapsed_ms = max(0.0, elapsed_s - start_offsets_s[car]) * 1000
            fraction = min(1.0, max(0.0, (car_elapsed_ms - prior) / duration))
            completed_dist = count
        fractions.append(fraction)
        distances.append(fraction * spec.track_length_m)
        total_distances.append((completed_dist + fraction) * spec.track_length_m)
    order = sorted(range(spec.cars), key=lambda car: (-total_distances[car], grid_positions[car]))
    positions = [0] * spec.cars
    for pos, car in enumerate(order, 1):
        positions[car] = pos

    packets: list[bytes] = []
    player_lap = lap_numbers[0]
    if frame_index % 5 == 0:
        sc_active = spec.sc_laps is not None and spec.sc_laps[0] <= player_lap <= spec.sc_laps[1]
        session: dict[str, object] = {
            "session_type": spec.session_type,
            "track_id": spec.track_id,
            "total_laps": spec.laps,
            "track_length": spec.track_length_m,
            "session_time_left": max(0, int((spec.laps * 120) - elapsed_s)),
            "safety_car_status": 2 if sc_active and spec.vsc else (1 if sc_active else 0),
        }
        packets.append(
            _packet_time(PacketId.SESSION, session, uid=uid, elapsed_s=elapsed_s, frame=frame)
        )

    cars_data: dict[int, dict[str, object]] = {}
    status_data: dict[int, dict[str, object]] = {}
    for car in range(spec.cars):
        lap = lap_numbers[car]
        count = completed[car]
        fraction = fractions[car]
        current_stops = stops[car]
        completed_stops = [
            (stop_lap, compound) for stop_lap, compound in current_stops if stop_lap < lap
        ]
        last_stop = completed_stops[-1][0] if completed_stops else 0
        compound = completed_stops[-1][1] if completed_stops else spec.start_compound
        tyre_age = min(255, max(0, lap - last_stop - 1))
        pit_in = next((stop_lap for stop_lap, _ in current_stops if stop_lap == lap), None)
        pit_out = next((stop_lap for stop_lap, _ in current_stops if stop_lap + 1 == lap), None)
        in_lane = pit_in is not None and fraction >= 0.85 and fraction < 1.0
        pit_area = pit_out is not None and fraction < 0.12
        pit_status = 1 if in_lane else (2 if pit_area else 0)
        previous_lap_ms = int(lap_times[car, count - 1]) if count else 0
        ahead = order.index(car) - 1
        gap_front = 0.0
        if ahead >= 0:
            front_car = order[ahead]
            gap_front = max(0.0, total_distances[front_car] - total_distances[car])
        leader = order[0]
        gap_leader = max(0.0, total_distances[leader] - total_distances[car])
        estimated_speed = spec.track_length_m / max(1.0, float(np.mean(lap_times[car])) / 1000.0)
        front_ms, front_min = _split_time(gap_front / estimated_speed * 1000)
        lead_ms, lead_min = _split_time(gap_leader / estimated_speed * 1000)
        elapsed_lap_ms = int(
            (float(lap_times[car, count]) if count < spec.laps else 0.0) * fraction
        )
        cars_data[car] = {
            "current_lap_num": lap,
            "car_position": positions[car],
            "grid_position": grid_positions[car],
            "lap_distance": distances[car],
            "total_distance": total_distances[car],
            "last_lap_time_ms": previous_lap_ms,
            "current_lap_time_ms": elapsed_lap_ms,
            "delta_to_car_in_front_ms_part": front_ms,
            "delta_to_car_in_front_minutes_part": front_min,
            "delta_to_race_leader_ms_part": lead_ms,
            "delta_to_race_leader_minutes_part": lead_min,
            "sector": min(2, int(fraction * 3)),
            "num_pit_stops": len(completed_stops),
            "pit_status": pit_status,
            "pit_lane_timer_active": int(in_lane),
            "pit_lane_time_in_lane_ms": 19_500 if pit_status else 0,
            "pit_stop_timer_ms": 0,
            "driver_status": 3 if in_lane else (2 if pit_area else 4),
            "result_status": 3 if count >= spec.laps else 2,
        }
        status: dict[str, object] = {
            "actual_tyre_compound": compound,
            "visual_tyre_compound": compound,
            "tyres_age_laps": tyre_age,
        }
        if car == 0:
            fuel = max(
                0.0,
                spec.priors.start_fuel_kg - spec.priors.fuel_kg_per_lap * (count + fraction),
            )
            status.update(
                fuel_in_tank=fuel,
                fuel_capacity=max(spec.priors.start_fuel_kg, 1.0),
                fuel_remaining_laps=fuel / max(spec.priors.fuel_kg_per_lap, 0.01)
                - max(0, spec.laps - lap + 1 - fraction),
                ers_store_energy=3_000_000.0,
            )
        status_data[car] = status

    packets.append(
        _packet_time(
            PacketId.LAP_DATA,
            {"cars": cars_data},
            uid=uid,
            elapsed_s=elapsed_s,
            frame=frame,
        )
    )
    packets.append(
        _packet_time(
            PacketId.CAR_STATUS,
            {"cars": status_data},
            uid=uid,
            elapsed_s=elapsed_s,
            frame=frame,
        )
    )
    player_age = int(cast(int, status_data[0]["tyres_age_laps"]))
    wear = min(100.0, player_age * 3.0)
    packets.append(
        _packet_time(
            PacketId.CAR_DAMAGE,
            {"cars": {0: {"tyres_wear": (wear,) * 4, "tyres_damage": (int(wear),) * 4}}},
            uid=uid,
            elapsed_s=elapsed_s,
            frame=frame,
        )
    )
    packets.append(
        _packet_time(
            PacketId.CAR_TELEMETRY,
            {
                "cars": {
                    0: {
                        "speed": 300,
                        "throttle": 0.8,
                        "engine_rpm": 11_000,
                        "tyres_surface_temperature": (90,) * 4,
                        "tyres_inner_temperature": (95,) * 4,
                        "tyres_pressure": (23.0,) * 4,
                    }
                }
            },
            uid=uid,
            elapsed_s=elapsed_s,
            frame=frame,
        )
    )
    history_car = frame_index % spec.cars
    packets.append(
        _packet_time(
            PacketId.SESSION_HISTORY,
            _session_history(
                history_car,
                completed[history_car],
                lap_times,
                stops[history_car],
                spec.start_compound,
                spec.sc_laps,
            ),
            uid=uid,
            elapsed_s=elapsed_s,
            frame=frame,
        )
    )
    return packets


def _recording_packets(spec: FieldSpec, uid: int) -> list[tuple[float, bytes]]:
    lap_times, stops = _build_lap_times(spec)
    records: list[tuple[float, bytes]] = []
    frame = 1

    def emit(packet_id: int, data: dict[str, object], elapsed_s: float = 0.0) -> None:
        nonlocal frame
        records.append(
            (
                elapsed_s,
                _packet_time(packet_id, data, uid=uid, elapsed_s=elapsed_s, frame=frame),
            )
        )
        frame += 1

    participants = {
        car: {
            "ai_controlled": int(car != 0),
            "driver_id": car,
            "team_id": _TEAM_IDS[car],
            "race_number": car + 1,
            "name": _DRIVER_NAMES[car].encode("ascii")[:32],
            "my_team": int(car == 0),
        }
        for car in range(spec.cars)
    }
    emit(PacketId.PARTICIPANTS, {"num_active_cars": spec.cars, "cars": participants})
    emit(PacketId.EVENT, {"event_string_code": b"LGOT"})

    events: set[str] = set()
    total_ms = float(np.max(np.sum(lap_times, axis=1, dtype=np.float32)))
    start_offsets_s = _grid_start_offsets(spec)
    duration_ms = (
        total_ms + max(start_offsets_s, default=0.0) * 1000 + (4_000.0 if spec.finish else 0.0)
    )
    frame_count = int(math.ceil(duration_ms / (spec.dt * 1000.0))) + 1
    for frame_index in range(frame_count):
        elapsed_s = frame_index * spec.dt
        player_cumulative = np.cumsum(lap_times[0], dtype=np.float32)
        player_elapsed_s = max(0.0, elapsed_s - start_offsets_s[0])
        player_completed = int(
            np.searchsorted(player_cumulative, player_elapsed_s * 1000, side="right")
        )
        player_lap = min(spec.laps + 1, player_completed + 1)
        if spec.sc_laps is not None:
            start, end = spec.sc_laps
            if player_lap == start and "deployed" not in events:
                events.add("deployed")
                emit(
                    PacketId.EVENT,
                    {
                        "event_string_code": b"SCAR",
                        "event_data": struct.pack("<BB", 2 if spec.vsc else 1, 0),
                    },
                    elapsed_s,
                )
            if player_lap == end and "returning" not in events and player_completed < spec.laps:
                events.add("returning")
                emit(
                    PacketId.EVENT,
                    {
                        "event_string_code": b"SCAR",
                        "event_data": struct.pack("<BB", 2 if spec.vsc else 1, 1),
                    },
                    elapsed_s,
                )
            if player_lap > end and "returned" not in events:
                events.add("returned")
                emit(
                    PacketId.EVENT,
                    {
                        "event_string_code": b"SCAR",
                        "event_data": struct.pack("<BB", 2 if spec.vsc else 1, 2),
                    },
                    elapsed_s,
                )
        packets = _frame_packets(spec, lap_times, stops, uid, elapsed_s, frame, frame_index)
        records.extend((elapsed_s, packet) for packet in packets)
        frame += 1
    finish_time_s = max(duration_ms / 1000.0, (frame_count - 1) * spec.dt)
    if spec.finish:
        emit(PacketId.EVENT, {"event_string_code": b"CHQF"}, finish_time_s)
    if spec.send_session_end:
        emit(PacketId.EVENT, {"event_string_code": b"SEND"}, finish_time_s)
    return records


def write_field_recording(spec: FieldSpec, out_dir: Path | str) -> GeneratedRace:
    """Write one reproducible synthetic field recording and return its identifiers."""
    body, spec_hash = _canonical_spec(spec)
    uid = synthetic_uid(bytes.fromhex(spec_hash))
    folder = Path(out_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"synth_t{spec.track_id}_{spec_hash[:10]}_s{spec.seed}"
    raw_path = folder / f"{stem}.f1bin"
    metadata: dict[str, Any] = {
        "synthetic": True,
        "generator": "field",
        "spec_hash": spec_hash,
        "spec": json.loads(body),
    }
    wall_clock_start_us = 1_700_000_000_000_000 + (spec.seed & 0xFFFF_FFFF)
    with RecordingWriter(
        raw_path,
        session_uid=uid,
        metadata=metadata,
        wall_clock_start_us=wall_clock_start_us,
    ) as writer:
        for elapsed_s, packet in _recording_packets(spec, uid):
            writer.write_datagram(elapsed_s, packet)
    compressed = compress_recording(raw_path, remove=True)
    digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
    return GeneratedRace(compressed, uid, digest, spec_hash, spec.seed)
