"""Offline Monte Carlo race rollouts."""

from __future__ import annotations

import importlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pitwall.synth.field import Priors


@dataclass(frozen=True, slots=True)
class RolloutSpec:
    laps: int
    cars: int = 20
    priors: Priors = field(default_factory=Priors)
    pace_spread_ms: float = 1200.0
    lap_noise_ms: float = 150.0
    sc_prob_per_lap: float = 0.04
    sc_len_laps: int = 2
    player_grid: int | None = None
    player_green_stop_lap: int | None = None
    start_compound: int = 17
    stop_compound: int = 18
    rival_two_stop_prob: float = 0.3
    rival_sc_stop_prob: float = 0.6

    def __post_init__(self) -> None:
        if self.laps < 2:
            raise ValueError("rollout laps must be at least 2")
        if self.cars < 2:
            raise ValueError("rollout cars must be at least 2")
        if self.player_grid is None:
            object.__setattr__(self, "player_grid", (self.cars + 1) // 2)
        elif not 1 <= self.player_grid <= self.cars:
            raise ValueError("player_grid must be within the field")
        if self.sc_len_laps < 1:
            raise ValueError("sc_len_laps must be positive")
        if not 0 <= self.sc_prob_per_lap <= 1:
            raise ValueError("sc_prob_per_lap must be between 0 and 1")
        if not 0 <= self.rival_two_stop_prob <= 1:
            raise ValueError("rival_two_stop_prob must be between 0 and 1")
        if not 0 <= self.rival_sc_stop_prob <= 1:
            raise ValueError("rival_sc_stop_prob must be between 0 and 1")
        if self.pace_spread_ms < 0 or self.lap_noise_ms < 0:
            raise ValueError("pace spread and lap noise must be non-negative")

    @property
    def player_grid_position(self) -> int:
        return self.player_grid if self.player_grid is not None else (self.cars + 1) // 2


@dataclass(frozen=True, slots=True)
class RolloutResult:
    mean_finish_position: float
    std_finish_position: float
    mean_race_time_s: float
    gain_probability: float
    sc_stop_rate: float
    sims: int
    device: str
    elapsed_s: float


def _threshold(thresholds: Mapping[str, float], name: str, default: float) -> float:
    value = thresholds.get(name, default)
    return float(value)


def _chunk_limit(spec: RolloutSpec, sims: int, chunk: int | None) -> int:
    float_items = spec.cars * spec.laps * 7 + spec.cars * 8 + 64
    bytes_per_sim = float_items * np.dtype(np.float32).itemsize
    limit = max(1, 1_000_000_000 // bytes_per_sim)
    return max(1, min(sims, chunk if chunk is not None else limit, limit))


def _uniform_layout(spec: RolloutSpec) -> tuple[dict[str, slice], int]:
    layout: dict[str, slice] = {}
    offset = 0

    def take(name: str, count: int) -> None:
        nonlocal offset
        layout[name] = slice(offset, offset + count)
        offset += count

    rivals = spec.cars - 1
    take("pace", 2 * ((rivals + 1) // 2))
    noise_count = spec.cars * spec.laps
    take("noise", 2 * ((noise_count + 1) // 2))
    take("first_stop", rivals)
    take("two_stop", rivals)
    take("second_stop", rivals)
    take("sc_presence", 1)
    take("sc_start", 1)
    take("sc_reaction", rivals)
    return layout, offset


def _normal_from_uniforms(uniforms: np.ndarray, count: int) -> np.ndarray:
    pairs = uniforms.reshape(uniforms.shape[0], -1, 2)
    first = np.maximum(pairs[:, :, 0], np.finfo(np.float32).tiny)
    radius = np.sqrt(-2.0 * np.log(first))
    angle = np.float32(2.0 * np.pi) * pairs[:, :, 1]
    values = np.stack((radius * np.cos(angle), radius * np.sin(angle)), axis=2)
    return values.reshape(uniforms.shape[0], -1)[:, :count].astype(np.float32, copy=False)


def _pace_offsets(spec: RolloutSpec, uniforms: np.ndarray, sims: int) -> np.ndarray:
    rivals = np.sort(
        _normal_from_uniforms(uniforms, spec.cars - 1) * np.float32(spec.pace_spread_ms),
        axis=1,
    )
    grid = spec.player_grid_position - 1
    if grid == 0:
        player = rivals[:, 0] - spec.pace_spread_ms * 0.1
    elif grid >= spec.cars - 1:
        player = rivals[:, -1] + spec.pace_spread_ms * 0.1
    else:
        player = (rivals[:, grid - 1] + rivals[:, grid]) / 2
    return np.concatenate((player[:, None], rivals), axis=1).astype(np.float32)


def _stop_plans(
    spec: RolloutSpec,
    sims: int,
    pit_min_laps_left: int,
    first_uniforms: np.ndarray,
    two_stop_uniforms: np.ndarray,
    second_uniforms: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    stop1 = np.full((sims, spec.cars), -1, dtype=np.int32)
    stop2 = np.full((sims, spec.cars), -1, dtype=np.int32)
    planned = spec.player_green_stop_lap
    if planned is None:
        planned = max(2, round(spec.laps * 0.55))
    planned_index = planned - 1
    pit_limit = spec.laps - pit_min_laps_left - 1
    if 0 <= planned_index <= pit_limit:
        stop1[:, 0] = planned_index
    if spec.laps < 5:
        return stop1, stop2
    first = np.rint((0.35 + 0.25 * first_uniforms) * spec.laps).astype(np.int32) - 1
    first = np.clip(first, 1, pit_limit)
    two_stop = two_stop_uniforms < spec.rival_two_stop_prob
    second = np.rint((0.70 + 0.15 * second_uniforms) * spec.laps).astype(np.int32) - 1
    second = np.maximum(second, first + 2)
    second = np.minimum(second, pit_limit)
    stop1[:, 1:] = first
    stop2[:, 1:] = np.where(two_stop & (second > first), second, -1)
    return stop1, stop2


def _safety_car(
    spec: RolloutSpec,
    sims: int,
    presence_uniforms: np.ndarray,
    start_uniforms: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    starts = np.full(sims, -1, dtype=np.int32)
    ends = np.full(sims, -1, dtype=np.int32)
    active = np.zeros((sims, spec.laps), dtype=np.bool_)
    if spec.sc_prob_per_lap == 0:
        return starts, ends, active
    probability = 1 - (1 - spec.sc_prob_per_lap) ** spec.laps
    has_sc = presence_uniforms < probability
    available = max(1, spec.laps - spec.sc_len_laps + 1)
    sampled_starts = np.floor(start_uniforms * available).astype(np.int32)
    starts[has_sc] = sampled_starts[has_sc]
    ends[has_sc] = np.minimum(starts[has_sc] + spec.sc_len_laps - 1, spec.laps - 1)
    for index in np.flatnonzero(has_sc):
        active[index, starts[index] : ends[index] + 1] = True
    return starts, ends, active


def _free_stop_policy(
    spec: RolloutSpec,
    thresholds: Mapping[str, float],
    stops: np.ndarray,
    sc_starts: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    policy_stop = np.zeros(len(sc_starts), dtype=np.bool_)
    planned = spec.player_green_stop_lap
    if planned is None:
        planned = max(2, round(spec.laps * 0.55))
    planned_index = planned - 1
    if planned_index < 0 or planned_index >= spec.laps:
        return policy_stop, stops[:, 0]
    sc_min = int(_threshold(thresholds, "sc_stop_min_laps_left", 3))
    pit_min = int(_threshold(thresholds, "pit_min_laps_left", 2))
    stint_min = int(_threshold(thresholds, "plan_min_stint_laps", 3))
    margin_ms = _threshold(thresholds, "free_stop_margin_s", 1.0) * 1000
    age = np.where(sc_starts < planned_index, sc_starts, planned_index)
    laps_left = spec.laps - (sc_starts + 1)
    deg = float(spec.priors.deg_ms_per_lap.get(spec.start_compound, 80.0))
    extra_wear_cost = np.maximum(0, planned_index - sc_starts) * deg * 0.5
    saving = spec.priors.pit_loss_green_ms - spec.priors.pit_loss_sc_ms - extra_wear_cost
    policy_stop = (
        (sc_starts >= 0)
        & (sc_starts < planned_index)
        & (age >= stint_min)
        & (laps_left >= sc_min)
        & (laps_left >= pit_min)
        & (saving > margin_ms)
    )
    player_stop = stops[:, 0].copy()
    player_stop[policy_stop] = sc_starts[policy_stop]
    return policy_stop, player_stop


def _lap_times_numpy(
    spec: RolloutSpec,
    thresholds: Mapping[str, float],
    uniforms: np.ndarray,
    layout: Mapping[str, slice],
) -> tuple[np.ndarray, np.ndarray]:
    sims = uniforms.shape[0]
    offsets = _pace_offsets(spec, uniforms[:, layout["pace"]], sims)
    pit_min = int(_threshold(thresholds, "pit_min_laps_left", 2))
    stop1, stop2 = _stop_plans(
        spec,
        sims,
        pit_min,
        uniforms[:, layout["first_stop"]],
        uniforms[:, layout["two_stop"]],
        uniforms[:, layout["second_stop"]],
    )
    sc_starts, _sc_ends, sc_active = _safety_car(
        spec,
        sims,
        uniforms[:, layout["sc_presence"]].ravel(),
        uniforms[:, layout["sc_start"]].ravel(),
    )
    policy_stop, player_stop = _free_stop_policy(spec, thresholds, stop1, sc_starts)
    stop1[:, 0] = player_stop
    stop2[:, 0] = -1
    if spec.laps >= 5:
        rival_sc_reaction = uniforms[:, layout["sc_reaction"]] < spec.rival_sc_stop_prob
        can_react = (
            (sc_starts[:, None] >= int(_threshold(thresholds, "plan_min_stint_laps", 3)))
            & (stop1[:, 1:] >= sc_starts[:, None])
            & (stop1[:, 1:] >= 0)
        )
        stop1[:, 1:] = np.where(can_react & rival_sc_reaction, sc_starts[:, None], stop1[:, 1:])

    stop_in_sc = np.zeros(sims, dtype=np.bool_)
    stop_in_sc[policy_stop] = True
    if spec.laps:
        player_stops = stop1[:, 0]
        valid = (player_stops >= 0) & (player_stops < spec.laps)
        rows = np.flatnonzero(valid)
        stop_in_sc[rows] |= sc_active[rows, player_stops[rows]]

    times = np.empty((sims, spec.cars, spec.laps), dtype=np.float32)
    cumulative = np.zeros((sims, spec.cars), dtype=np.float32)
    last_stop = np.full((sims, spec.cars), -1, dtype=np.int32)
    compound = np.full((sims, spec.cars), spec.start_compound, dtype=np.int32)
    deg_values = spec.priors.deg_ms_per_lap
    base_ms = np.float32(spec.priors.base_ms)
    fuel_rate = np.float32(spec.priors.fuel_ms_per_kg)
    noise = _normal_from_uniforms(uniforms[:, layout["noise"]], spec.cars * spec.laps).reshape(
        sims, spec.cars, spec.laps
    )
    for lap in range(spec.laps):
        age = np.maximum(0, lap - last_stop - 1)
        deg = np.full((sims, spec.cars), 80.0, dtype=np.float32)
        for compound_id, value in deg_values.items():
            deg[compound == int(compound_id)] = float(value)
        fuel_kg = np.maximum(0.0, spec.priors.start_fuel_kg - spec.priors.fuel_kg_per_lap * lap)
        lap_ms = (
            base_ms
            + offsets
            + deg * age
            + fuel_rate * np.float32(fuel_kg)
            + noise[:, :, lap] * np.float32(spec.lap_noise_ms)
        )
        pit_now = (stop1 == lap) | (stop2 == lap)
        pit_prev = (stop1 == lap - 1) | (stop2 == lap - 1)
        green_loss = np.float32(spec.priors.pit_loss_green_ms)
        sc_loss = np.float32(spec.priors.pit_loss_sc_ms)
        pit_loss = np.where(sc_active[:, lap, None], sc_loss, green_loss)
        lap_ms += pit_now * pit_loss * np.float32(0.4)
        if lap > 0:
            lap_ms += (
                pit_prev
                * np.where(sc_active[:, lap - 1, None], sc_loss, green_loss)
                * np.float32(0.6)
            )
        if sc_active[:, lap].any():
            lap_ms = np.where(sc_active[:, lap, None], lap_ms * np.float32(1.4), lap_ms)
        times[:, :, lap] = lap_ms
        cumulative += lap_ms
        if sc_active[:, lap].any():
            rows = np.flatnonzero(sc_active[:, lap])
            order = np.argsort(cumulative[rows], axis=1, kind="stable")
            sorted_times = np.take_along_axis(cumulative[rows], order, axis=1)
            target = np.minimum(
                sorted_times,
                sorted_times[:, :1] + np.arange(spec.cars, dtype=np.float32) * 800.0,
            )
            adjusted = np.empty_like(cumulative[rows])
            np.put_along_axis(adjusted, order, target, axis=1)
            times[rows, :, lap] += adjusted - cumulative[rows]
            cumulative[rows] = adjusted
        last_stop = np.where(pit_now, lap, last_stop)
        compound = np.where(stop1 == lap, spec.stop_compound, compound)
        compound = np.where(stop2 == lap, spec.start_compound, compound)
    return cumulative, stop_in_sc


def _torch_backend(
    spec: RolloutSpec,
    thresholds: Mapping[str, float],
    sims: int,
    seed: int,
    chunk_size: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    torch = importlib.import_module("torch")
    generator = torch.Generator(device="cuda")
    generator.manual_seed(seed)
    finishes: list[np.ndarray] = []
    sc_stop_samples: list[np.ndarray] = []
    race_times: list[np.ndarray] = []
    planned = spec.player_green_stop_lap or max(2, round(spec.laps * 0.55))
    planned_index = planned - 1
    sc_min = int(_threshold(thresholds, "sc_stop_min_laps_left", 3))
    pit_min = int(_threshold(thresholds, "pit_min_laps_left", 2))
    stint_min = int(_threshold(thresholds, "plan_min_stint_laps", 3))
    margin_ms = _threshold(thresholds, "free_stop_margin_s", 1.0) * 1000
    probability = 1 - (1 - spec.sc_prob_per_lap) ** spec.laps
    for start_sim in range(0, sims, chunk_size):
        size = min(chunk_size, sims - start_sim)
        rivals = (
            torch.randn(
                (size, spec.cars - 1), generator=generator, device="cuda", dtype=torch.float32
            )
            * spec.pace_spread_ms
        )
        rivals, _ = torch.sort(rivals, dim=1)
        grid = spec.player_grid_position - 1
        if grid == 0:
            player = rivals[:, 0] - spec.pace_spread_ms * 0.1
        elif grid >= spec.cars - 1:
            player = rivals[:, -1] + spec.pace_spread_ms * 0.1
        else:
            player = (rivals[:, grid - 1] + rivals[:, grid]) / 2
        offsets = torch.cat((player[:, None], rivals), dim=1)
        stop1 = torch.full((size, spec.cars), -1, device="cuda", dtype=torch.int64)
        stop2 = torch.full_like(stop1, -1)
        pit_limit = spec.laps - pit_min - 1
        if 0 <= planned_index <= pit_limit:
            stop1[:, 0] = planned_index
        sc_draw = torch.rand((size,), generator=generator, device="cuda")
        has_sc = sc_draw < probability
        available = max(1, spec.laps - spec.sc_len_laps + 1)
        sc_start = torch.randint(
            available, (size,), generator=generator, device="cuda", dtype=torch.int64
        )
        sc_start = torch.where(has_sc, sc_start, torch.full_like(sc_start, -1))
        sc_end = torch.minimum(sc_start + spec.sc_len_laps - 1, spec.laps - 1)
        first = (
            torch.rand((size, spec.cars - 1), generator=generator, device="cuda") * 0.25 + 0.35
        ) * spec.laps
        first = torch.clamp(torch.round(first).to(torch.int64) - 1, 1, pit_limit)
        two = (
            torch.rand((size, spec.cars - 1), generator=generator, device="cuda")
            < spec.rival_two_stop_prob
        )
        second = (
            torch.rand((size, spec.cars - 1), generator=generator, device="cuda") * 0.15 + 0.70
        ) * spec.laps
        second = torch.maximum(torch.round(second).to(torch.int64) - 1, first + 2)
        second = torch.minimum(second, torch.full_like(second, pit_limit))
        if spec.laps >= 5:
            stop1[:, 1:] = first
            stop2[:, 1:] = torch.where(two & (second > first), second, -1)
            reaction = (
                torch.rand((size, spec.cars - 1), generator=generator, device="cuda")
                < spec.rival_sc_stop_prob
            )
            can_react = (sc_start[:, None] >= stint_min) & (stop1[:, 1:] >= sc_start[:, None])
            stop1[:, 1:] = torch.where(can_react & reaction, sc_start[:, None], stop1[:, 1:])
        age_at_sc = torch.minimum(sc_start, torch.full_like(sc_start, planned_index))
        laps_left = spec.laps - (sc_start + 1)
        deg = float(spec.priors.deg_ms_per_lap.get(spec.start_compound, 80.0))
        extra_cost = torch.clamp(planned_index - sc_start, min=0) * deg * 0.5
        saving = spec.priors.pit_loss_green_ms - spec.priors.pit_loss_sc_ms - extra_cost
        policy_stop = (
            (sc_start >= 0)
            & (sc_start < planned_index)
            & (age_at_sc >= stint_min)
            & (laps_left >= sc_min)
            & (laps_left >= pit_min)
            & (saving > margin_ms)
        )
        if 0 <= planned_index < spec.laps:
            stop1[:, 0] = torch.where(policy_stop, sc_start, stop1[:, 0])
        stop_in_sc = policy_stop.clone()
        compound = torch.full(
            (size, spec.cars), spec.start_compound, device="cuda", dtype=torch.int64
        )
        last_stop = torch.full_like(stop1, -1)
        cumulative = torch.zeros((size, spec.cars), device="cuda", dtype=torch.float32)
        lap_times = torch.empty((size, spec.cars, spec.laps), device="cuda", dtype=torch.float32)
        for lap in range(spec.laps):
            age = torch.clamp(lap - last_stop - 1, min=0).to(torch.float32)
            deg_array = torch.full_like(age, 80.0)
            for compound_id, value in spec.priors.deg_ms_per_lap.items():
                deg_array = torch.where(compound == int(compound_id), float(value), deg_array)
            noise = (
                torch.randn(
                    (size, spec.cars), generator=generator, device="cuda", dtype=torch.float32
                )
                * spec.lap_noise_ms
            )
            fuel = max(
                0.0,
                spec.priors.start_fuel_kg - spec.priors.fuel_kg_per_lap * lap,
            )
            lap_ms = (
                spec.priors.base_ms
                + offsets
                + deg_array * age
                + spec.priors.fuel_ms_per_kg * fuel
                + noise
            )
            sc_now = (sc_start >= 0) & (lap >= sc_start) & (lap <= sc_end)
            pit_now = (stop1 == lap) | (stop2 == lap)
            pit_prev = (stop1 == lap - 1) | (stop2 == lap - 1)
            loss_now = torch.where(
                sc_now, spec.priors.pit_loss_sc_ms, spec.priors.pit_loss_green_ms
            )[:, None]
            lap_ms = lap_ms + pit_now * loss_now * 0.4
            if lap > 0:
                sc_prev = (sc_start >= 0) & (lap - 1 >= sc_start) & (lap - 1 <= sc_end)
                loss_prev = torch.where(
                    sc_prev, spec.priors.pit_loss_sc_ms, spec.priors.pit_loss_green_ms
                )[:, None]
                lap_ms = lap_ms + pit_prev * loss_prev * 0.6
            lap_ms = torch.where(sc_now[:, None], lap_ms * 1.4, lap_ms)
            lap_times[:, :, lap] = lap_ms
            cumulative += lap_ms
            if torch.any(sc_now):
                rows = torch.nonzero(sc_now, as_tuple=True)[0]
                if rows.numel():
                    order = torch.argsort(cumulative[rows], dim=1, stable=True)
                    sorted_times = torch.gather(cumulative[rows], 1, order)
                    rank_gap = (
                        torch.arange(spec.cars, dtype=torch.float32, device="cuda")[None, :] * 800.0
                    )
                    target = torch.minimum(sorted_times, sorted_times[:, :1] + rank_gap)
                    adjusted = torch.empty_like(cumulative[rows]).scatter(1, order, target)
                    lap_times[rows, :, lap] += adjusted - cumulative[rows]
                    cumulative[rows] = adjusted
            stops = pit_now
            last_stop = torch.where(stops, torch.full_like(last_stop, lap), last_stop)
            compound = torch.where(stop1 == lap, spec.stop_compound, compound)
            compound = torch.where(stop2 == lap, spec.start_compound, compound)
        player_sc = (stop1[:, 0] >= 0) & (stop1[:, 0] <= sc_end) & (stop1[:, 0] >= sc_start)
        stop_in_sc |= player_sc
        player_time = cumulative[:, 0]
        order_position = 1 + torch.sum(cumulative[:, 1:] < player_time[:, None], dim=1)
        finishes.append(order_position.to("cpu").numpy())
        sc_stop_samples.append(stop_in_sc.to("cpu").numpy())
        race_times.append(player_time.to("cpu").numpy())
    return (
        np.concatenate(finishes),
        np.concatenate(sc_stop_samples),
        np.concatenate(race_times),
    )


def run_rollouts(
    spec: RolloutSpec,
    thresholds: Mapping[str, float],
    sims: int,
    seed: int,
    device: str = "cpu",
    chunk: int | None = None,
) -> RolloutResult:
    """Run seeded race simulations without importing the live engine."""
    if sims < 1:
        raise ValueError("sims must be positive")
    if device not in {"cpu", "cuda", "auto"}:
        raise ValueError("device must be cpu, cuda, or auto")
    selected = device
    torch: Any = None
    if device in {"cuda", "auto"}:
        try:
            torch = importlib.import_module("torch")
        except ImportError:
            if device == "cuda":
                raise RuntimeError(
                    "CUDA rollouts require the optional PyTorch GPU install"
                ) from None
        if torch is not None and torch.cuda.is_available():
            selected = "cuda"
        elif device == "cuda":
            raise RuntimeError("CUDA rollouts requested, but CUDA is unavailable")
        else:
            selected = "cpu"
    chunk_size = _chunk_limit(spec, sims, chunk)
    started = time.perf_counter()
    if selected == "cuda":
        finish_positions, sc_stops, race_times = _torch_backend(
            spec, thresholds, sims, seed, chunk_size
        )
    else:
        rng = np.random.default_rng(seed)
        layout, draw_count = _uniform_layout(spec)
        finish_parts: list[np.ndarray] = []
        stop_parts: list[np.ndarray] = []
        time_parts: list[np.ndarray] = []
        for start in range(0, sims, chunk_size):
            size = min(chunk_size, sims - start)
            uniforms = rng.random((size, draw_count), dtype=np.float32)
            cumulative, sc_stops_chunk = _lap_times_numpy(spec, thresholds, uniforms, layout)
            finish = 1 + np.sum(cumulative[:, 1:] < cumulative[:, :1], axis=1)
            finish_parts.append(finish.astype(np.float32))
            stop_parts.append(sc_stops_chunk)
            time_parts.append(cumulative[:, 0])
        finish_positions = np.concatenate(finish_parts)
        sc_stops = np.concatenate(stop_parts)
        race_times = np.concatenate(time_parts)
    elapsed = time.perf_counter() - started
    return RolloutResult(
        mean_finish_position=float(np.mean(finish_positions)),
        std_finish_position=float(np.std(finish_positions)),
        mean_race_time_s=float(np.mean(race_times) / 1000),
        gain_probability=float(np.mean(finish_positions < spec.player_grid_position)),
        sc_stop_rate=float(np.mean(sc_stops)),
        sims=sims,
        device=selected,
        elapsed_s=elapsed,
    )
