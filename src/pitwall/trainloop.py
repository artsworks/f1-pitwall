"""Bounded, read-only offline threshold search."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import yaml

from pitwall.bench import (
    build_recording_index,
    build_scorecard,
    compare,
    find_source,
    load_scenarios,
    run_scenarios,
)
from pitwall.config.loader import ConfigStore
from pitwall.rollout import RolloutSpec, run_rollouts
from pitwall.synth.field import Priors


def _parse_grid(values: list[str]) -> list[dict[str, float]]:
    dimensions: list[tuple[str, list[float]]] = []
    for assignment in values:
        name, separator, raw_values = assignment.partition("=")
        if not separator or not name:
            raise ValueError(f"invalid parameter {assignment!r}, expected NAME=v1,v2")
        parsed = [float(value) for value in raw_values.split(",") if value]
        if not parsed:
            raise ValueError(f"parameter {name!r} has no values")
        dimensions.append((name, parsed))
    if not dimensions:
        raise ValueError("at least one --param grid is required")
    return [
        dict(zip((name for name, _ in dimensions), values, strict=True))
        for values in itertools.product(*(values for _, values in dimensions))
    ]


def _rollout_spec(path: Path) -> RolloutSpec:
    from pitwall.net.recording import RecordingReader
    from pitwall.protocol.header import PacketId, parse_header
    from pitwall.protocol.packets import parse

    with RecordingReader(path) as reader:
        raw_spec = reader.header.metadata.get("spec")
        if isinstance(raw_spec, dict):
            priors_data = raw_spec.get("priors", {})
            degrees = priors_data.get("deg_ms_per_lap", {}) if isinstance(priors_data, dict) else {}
            priors = Priors(
                base_ms=float(priors_data.get("base_ms", 90_000.0)),
                deg_ms_per_lap={int(key): float(value) for key, value in degrees.items()},
                fuel_kg_per_lap=float(priors_data.get("fuel_kg_per_lap", 1.7)),
                fuel_ms_per_kg=float(priors_data.get("fuel_ms_per_kg", 30.0)),
                start_fuel_kg=float(priors_data.get("start_fuel_kg", 90.0)),
                pit_loss_green_ms=float(priors_data.get("pit_loss_green_ms", 22_000.0)),
                pit_loss_sc_ms=float(priors_data.get("pit_loss_sc_ms", 8_000.0)),
                pit_loss_vsc_ms=float(priors_data.get("pit_loss_vsc_ms", 12_000.0)),
            )
            stops = raw_spec.get("player_stops", [])
            return RolloutSpec(
                laps=int(raw_spec["laps"]),
                cars=int(raw_spec["cars"]),
                priors=priors,
                pace_spread_ms=float(raw_spec.get("pace_spread_ms", 1200.0)),
                lap_noise_ms=float(raw_spec.get("lap_noise_ms", 150.0)),
                player_grid=min(
                    max(int(raw_spec.get("grid_position", 10)), 1), int(raw_spec["cars"])
                ),
                player_green_stop_lap=int(stops[0][0]) if stops else None,
                start_compound=int(raw_spec.get("start_compound", 17)),
                stop_compound=int(stops[0][1]) if stops else 18,
            )
        laps = 17
        cars = 20
        grid = 10
        for _offset, payload in reader:
            header = parse_header(payload)
            packet = parse(header.packet_id, payload, header)
            if header.packet_id == PacketId.SESSION:
                laps = int(packet.total_laps or laps)
            elif header.packet_id == PacketId.PARTICIPANTS:
                cars = int(packet.num_active_cars or cars)
            elif header.packet_id == PacketId.LAP_DATA:
                grid = int(cast(int, packet.cars[0].grid_position or grid))
        return RolloutSpec(
            laps=max(laps, 2),
            cars=max(cars, 2),
            player_grid=min(max(grid, 1), max(cars, 2)),
        )


def run_training_loop(
    args: argparse.Namespace,
    *,
    clock: Callable[[], float] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Try seeded threshold candidates without writing scenarios or baselines."""
    now = clock or time.monotonic
    started = now()
    scenarios_dir = Path(args.scenarios).expanduser()
    baseline_path = (
        Path(args.baseline).expanduser() if args.baseline else scenarios_dir / "baseline.json"
    )
    try:
        candidates = _parse_grid(args.param)
        if args.max_iters is not None and args.max_iters < 1:
            raise ValueError("--max-iters must be positive")
        if args.max_minutes is not None and args.max_minutes <= 0:
            raise ValueError("--max-minutes must be positive")
        if args.jobs < 1 or args.sims < 1:
            raise ValueError("--jobs and --sims must be positive")
        scenarios = load_scenarios(scenarios_dir)
        if args.target not in {scenario.id for scenario in scenarios}:
            raise ValueError(f"target scenario not found: {args.target}")
        recording_dirs = [Path(path).expanduser() for path in args.recordings]
        if not recording_dirs:
            recording_dirs = [Path(ConfigStore().current().recording.directory).expanduser()]
        index = build_recording_index(recording_dirs)
        baseline: dict[str, Any] | None = None
        if baseline_path.exists():
            loaded = json.loads(baseline_path.read_text())
            if not isinstance(loaded, dict):
                raise ValueError(f"{baseline_path}: baseline must be a JSON object")
            baseline = loaded
        default_settings = ConfigStore(isolated=True).current()
        rule_count = len(default_settings.rules)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return 1, {"error": str(exc), "tried": [], "untried": []}

    random.Random(args.seed).shuffle(candidates)
    reports: list[dict[str, Any]] = []
    stop_reason = "candidates_exhausted"
    source_missing = False
    target = next(scenario for scenario in scenarios if scenario.id == args.target)
    target_source, _ = find_source(target, index)

    for candidate in candidates:
        if args.max_iters is not None and len(reports) >= args.max_iters:
            stop_reason = "max_iters"
            break
        if args.max_minutes is not None and (now() - started) >= args.max_minutes * 60:
            stop_reason = "max_minutes"
            break
        with tempfile.TemporaryDirectory(prefix="pitwall-train-") as temporary:
            rules_dir = Path(temporary)
            (rules_dir / "thresholds.yaml").write_text(
                yaml.safe_dump({"thresholds": candidate}, sort_keys=True), encoding="utf-8"
            )
            try:
                settings = ConfigStore(rules_dir=rules_dir, isolated=True).current()
                if len(settings.rules) != rule_count:
                    raise RuntimeError("temporary thresholds changed the packaged rule count")
                rows, outcomes = run_scenarios(scenarios, index, settings, rules_dir, args.jobs)
                scorecard = build_scorecard(rows, outcomes)
                gate = compare(
                    scorecard,
                    baseline,
                    scenario_ids={scenario.id for scenario in scenarios},
                )
                missing = any(row["result"] == "skipped" for row in rows.values())
                target_pass = rows.get(args.target, {}).get("result") == "pass"
                guard_failures = sum(
                    row["status"] == "guard" and row["result"] in {"fail", "error"}
                    for row in rows.values()
                )
                rollout: dict[str, Any] | None = None
                if target_source is not None:
                    rollout_result = run_rollouts(
                        _rollout_spec(target_source),
                        candidate,
                        sims=args.sims,
                        seed=args.seed,
                        device=args.device,
                    )
                    rollout = asdict(rollout_result)
                report = {
                    "parameters": candidate,
                    "gate": asdict(gate),
                    "target_pass": target_pass,
                    "guard_failures": guard_failures,
                    "rollout": rollout,
                    "scorecard": scorecard,
                }
            except (OSError, ValueError, RuntimeError) as exc:
                return 1, {
                    "error": str(exc),
                    "tried": reports,
                    "untried": candidates[len(reports) :],
                    "stop_reason": "candidate_error",
                }
        reports.append(report)
        if missing:
            source_missing = True
            stop_reason = "missing_sources"
            break
        if gate.status == "pass" and target_pass:
            stop_reason = "gate_and_target_passed"
            break

    if not source_missing and stop_reason == "candidates_exhausted":
        if (
            args.max_iters is not None
            and len(reports) >= args.max_iters
            and len(reports) < len(candidates)
        ):
            stop_reason = "max_iters"
        elif args.max_minutes is not None and (now() - started) >= args.max_minutes * 60:
            stop_reason = "max_minutes"
    ranked = sorted(
        reports,
        key=lambda report: (
            report["gate"]["status"] != "pass",
            not report["target_pass"],
            report["guard_failures"],
            report["rollout"]["mean_finish_position"]
            if report["rollout"] is not None
            else math.inf,
        ),
    )
    result = {
        "target": args.target,
        "stop_reason": stop_reason,
        "winner": ranked[0] if ranked else None,
        "tried": reports,
        "untried": candidates[len(reports) :],
        "elapsed_s": max(0.0, now() - started),
        "human_review": (
            "Apply the winner in one PR with pitwall bench --update-baseline --note. "
            "Test changes found only on synthetic races against real recordings."
        ),
    }
    if args.out:
        output = Path(args.out).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    if source_missing:
        return 2, result
    if any(report["gate"]["status"] == "pass" and report["target_pass"] for report in reports):
        return 0, result
    return 1, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded threshold search against pitwall bench."
    )
    parser.add_argument("--target", required=True)
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--scenarios", default="scenarios")
    parser.add_argument("--recordings", action="append", default=[])
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--max-iters", type=int, default=10)
    parser.add_argument("--max-minutes", type=float, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--sims", type=int, default=10_000)
    parser.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code, _report = run_training_loop(args)
    return code
