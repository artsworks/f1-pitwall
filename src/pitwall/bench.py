"""Scenario scorecards, baseline gates, and run trends."""

from __future__ import annotations

import hashlib
import math
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Literal

import yaml

from pitwall.config.models import Settings
from pitwall.derive import MutationOp, derive_recording, ops_from_options
from pitwall.net.recording import RecordingReader
from pitwall.store.db import Database
from pitwall.tune import _AUTO_SKIP_METRICS

CheckType = Literal["fire", "absent"]


@dataclass(frozen=True, slots=True)
class ScenarioCheck:
    type: CheckType
    rules: tuple[str, ...]
    laps: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    status: Literal["guard", "target"]
    source_uid: int
    source_sha256: str
    mutations: Mapping[str, Any]
    checks: tuple[ScenarioCheck, ...]

    @property
    def kind(self) -> Literal["real", "synthetic"]:
        return "synthetic" if self.mutations else "real"


@dataclass(frozen=True, slots=True)
class GateResult:
    status: Literal["pass", "fail", "incomplete"]
    failures: list[str]
    incomplete: list[str]
    improvements: list[str]


def _mapping(value: object, path: Path, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: {name} must be a mapping")
    return value


def _check_keys(value: Mapping[str, Any], allowed: set[str], path: Path, prefix: str = "") -> None:
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            name = f"{prefix}.{key}" if prefix else str(key)
            raise ValueError(f"{path}: unknown key {name}")


def _required(value: Mapping[str, Any], key: str, path: Path, prefix: str = "") -> Any:
    if key not in value:
        name = f"{prefix}.{key}" if prefix else key
        raise ValueError(f"{path}: missing key {name}")
    return value[key]


def _parse_uid(value: object, path: Path) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{path}: source.session_uid must be a hex string or integer")
    if isinstance(value, int):
        uid = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            uid = int(text, 0) if text.lower().startswith("0x") else int(text, 16)
        except ValueError as exc:
            raise ValueError(f"{path}: source.session_uid must be a hex string or integer") from exc
    else:
        raise ValueError(f"{path}: source.session_uid must be a hex string or integer")
    if not 0 <= uid <= 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError(f"{path}: source.session_uid must fit in an unsigned 64-bit integer")
    return uid


def _parse_check(value: object, kind: CheckType, path: Path, index: int) -> ScenarioCheck:
    prefix = f"expect.{kind}[{index}]"
    check = _mapping(value, path, prefix)
    _check_keys(check, {"rule", "laps"}, path, prefix)
    raw_rules = _required(check, "rule", path, prefix)
    if isinstance(raw_rules, str):
        rules = (raw_rules,)
    elif (
        isinstance(raw_rules, list)
        and raw_rules
        and all(isinstance(rule, str) for rule in raw_rules)
    ):
        rules = tuple(raw_rules)
    else:
        raise ValueError(f"{path}: {prefix}.rule must be a string or non-empty list of strings")
    raw_laps = _required(check, "laps", path, prefix)
    if (
        not isinstance(raw_laps, list)
        or len(raw_laps) != 2
        or any(isinstance(lap, bool) or not isinstance(lap, int) for lap in raw_laps)
    ):
        raise ValueError(f"{path}: {prefix}.laps must be [start, end] integers")
    start, end = raw_laps
    if start < 1 or end < start:
        raise ValueError(f"{path}: {prefix}.laps must satisfy 1 <= start <= end")
    return ScenarioCheck(kind, rules, (start, end))


def _scenario_ops(mutations: Mapping[str, Any], path: Path) -> list[MutationOp]:
    _check_keys(mutations, {"inject_sc", "vsc", "wear_scale", "penalty"}, path, "mutations")
    wear_scale = mutations.get("wear_scale")
    if wear_scale is not None and (
        isinstance(wear_scale, bool) or not isinstance(wear_scale, int | float)
    ):
        raise ValueError(f"{path}: mutations.wear_scale must be a number")
    inject_sc = mutations.get("inject_sc")
    if inject_sc is not None and not isinstance(inject_sc, str):
        raise ValueError(f"{path}: mutations.inject_sc must be a string")
    vsc = mutations.get("vsc", False)
    if not isinstance(vsc, bool):
        raise ValueError(f"{path}: mutations.vsc must be a boolean")
    penalty = mutations.get("penalty")
    if penalty is not None and (isinstance(penalty, bool) or not isinstance(penalty, int)):
        raise ValueError(f"{path}: mutations.penalty must be an integer")
    try:
        return (
            ops_from_options(
                float(wear_scale) if wear_scale is not None else None,
                inject_sc,
                vsc,
                penalty,
            )
            if mutations
            else []
        )
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def parse_scenario(path: Path) -> Scenario:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    data = _mapping(raw, path, "scenario")
    _check_keys(data, {"id", "title", "status", "source", "mutations", "expect", "why"}, path)
    scenario_id = _required(data, "id", path)
    title = _required(data, "title", path)
    status = _required(data, "status", path)
    if not isinstance(scenario_id, str) or scenario_id != path.stem:
        raise ValueError(f"{path}: id must equal file stem {path.stem!r}")
    if not isinstance(title, str):
        raise ValueError(f"{path}: title must be a string")
    if status not in ("guard", "target"):
        raise ValueError(f"{path}: status must be 'guard' or 'target'")
    source = _mapping(_required(data, "source", path), path, "source")
    _check_keys(source, {"session_uid", "sha256", "where"}, path, "source")
    source_uid = _parse_uid(_required(source, "session_uid", path, "source"), path)
    source_sha256 = _required(source, "sha256", path, "source")
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in source_sha256)
    ):
        raise ValueError(f"{path}: source.sha256 must be a 64-character hex digest")
    where = source.get("where")
    if where is not None and not isinstance(where, str):
        raise ValueError(f"{path}: source.where must be a string")
    why = data.get("why")
    if why is not None and not isinstance(why, str):
        raise ValueError(f"{path}: why must be a string")
    mutations_value = data.get("mutations", {})
    mutations = _mapping(mutations_value, path, "mutations")
    _scenario_ops(mutations, path)
    expected = _mapping(_required(data, "expect", path), path, "expect")
    _check_keys(expected, {"fire", "absent"}, path, "expect")
    checks: list[ScenarioCheck] = []
    check_types: tuple[CheckType, ...] = ("fire", "absent")
    for kind in check_types:
        raw_checks = expected.get(kind, [])
        if not isinstance(raw_checks, list):
            raise ValueError(f"{path}: expect.{kind} must be a list")
        checks.extend(
            _parse_check(raw_check, kind, path, index) for index, raw_check in enumerate(raw_checks)
        )
    if not checks:
        raise ValueError(f"{path}: expect must contain at least one fire or absent check")
    return Scenario(
        scenario_id,
        title,
        status,
        source_uid,
        source_sha256.lower(),
        dict(mutations),
        tuple(checks),
    )


def load_scenarios(directory: Path, only: Sequence[str] | None = None) -> list[Scenario]:
    scenarios = [
        parse_scenario(path) for path in sorted(directory.glob("*.yaml"), key=lambda p: p.stem)
    ]
    if only is None:
        return scenarios
    requested = set(only)
    available = {scenario.id for scenario in scenarios}
    missing = sorted(requested - available)
    if missing:
        raise ValueError(f"{directory}: scenario id not found: {', '.join(missing)}")
    return [scenario for scenario in scenarios if scenario.id in requested]


def build_recording_index(directories: Sequence[Path]) -> dict[int, list[Path]]:
    candidates: set[Path] = set()
    for directory in directories:
        root = directory.expanduser()
        if not root.is_dir():
            continue
        candidates.update(root.rglob("*.f1bin"))
        candidates.update(root.rglob("*.f1bin.zst"))
    by_uid: dict[int, list[Path]] = defaultdict(list)
    for path in sorted(candidates, key=str):
        try:
            with RecordingReader(path) as reader:
                by_uid[reader.header.session_uid].append(path)
        except Exception:
            continue
    return dict(by_uid)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def find_source(scenario: Scenario, index: Mapping[int, Sequence[Path]]) -> tuple[Path | None, str]:
    candidates = index.get(scenario.source_uid, ())
    if not candidates:
        return None, "source not found"
    for candidate in candidates:
        try:
            if _file_sha256(candidate) == scenario.source_sha256:
                return candidate, ""
        except OSError:
            continue
    return None, "sha256 mismatch"


def evaluate_checks(
    checks: Sequence[ScenarioCheck], fired: Sequence[tuple[str, int]]
) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []
    for check in checks:
        start, end = check.laps
        matching_laps = sorted({lap for rule, lap in fired if rule in check.rules})
        in_window = any(rule in check.rules and start <= lap <= end for rule, lap in fired)
        ok = in_window if check.type == "fire" else not in_window
        evaluated.append(
            {
                "type": check.type,
                "rules": list(check.rules),
                "laps": [start, end],
                "ok": ok,
                "found": matching_laps,
            }
        )
    return evaluated


def _scenario_row(
    scenario: Scenario,
    result: str,
    reason: str = "",
    checks: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if checks is None:
        checks = evaluate_checks(scenario.checks, ())
        if result == "error":
            checks = [{**check, "ok": False} for check in checks]
    return {
        "title": scenario.title,
        "status": scenario.status,
        "kind": scenario.kind,
        "result": result,
        "reason": reason,
        "checks": list(checks),
    }


def run_scenario(
    scenario: Scenario,
    source: Path | None,
    settings: Settings,
    rules_dir: Path | None = None,
    skip_reason: str = "source not found",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if source is None:
        return _scenario_row(scenario, "skipped", skip_reason), []
    try:
        from pitwall.ingest import ingest_recordings

        with tempfile.TemporaryDirectory(prefix="pitwall-bench-") as temporary:
            directory = Path(temporary)
            path = source
            uid = scenario.source_uid
            if scenario.kind == "synthetic":
                path = directory / "derived.f1bin.zst"
                summary = derive_recording(source, path, _scenario_ops(scenario.mutations, source))
                uid = summary.header.session_uid
            db = Database(directory / "bench.sqlite")
            try:
                ingested = ingest_recordings(
                    db,
                    [str(path)],
                    settings,
                    out_dir=directory / "digests",
                    rules_dir=rules_dir,
                    isolated=True,
                )
                if not ingested:
                    raise ValueError("ingest returned no result")
                if ingested[0].status == "error":
                    return _scenario_row(
                        scenario, "error", ingested[0].error or "ingest failed"
                    ), []
                calls = db.calls_for_session(uid)
                fired = [
                    (str(call["rule_id"]), int(call["lap"]))
                    for call in calls
                    if call.get("outcome") == "fired" and call.get("lap") is not None
                ]
                checks = evaluate_checks(scenario.checks, fired)
                result = "pass" if all(check["ok"] for check in checks) else "fail"
                outcomes: list[dict[str, Any]] = []
                if scenario.kind == "real":
                    outcomes = [
                        outcome
                        for outcome in db.all_outcomes()
                        if int(outcome["session_uid"]) == uid
                    ]
                return _scenario_row(scenario, result, checks=checks), outcomes
            finally:
                db.close()
    except Exception as exc:
        return _scenario_row(scenario, "error", str(exc)), []


def _real_metrics(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    good = 0
    wrong = 0
    by_rule: dict[str, dict[str, int]] = defaultdict(lambda: {"good": 0, "wrong": 0})
    stop_errors: list[float] = []
    pace_errors: list[float] = []
    for outcome in outcomes:
        label = str(outcome.get("label") or "")
        call_id = str(outcome.get("call_id") or "")
        metric = str(outcome.get("metric") or "")
        error = outcome.get("error")
        if label in ("good", "wrong") and error is not None:
            if metric == "stop_cost_s":
                stop_errors.append(abs(float(error)))
            elif metric == "laps_of_pace":
                pace_errors.append(abs(float(error)))
        if (
            label not in ("good", "wrong")
            or call_id.startswith("plan:")
            or metric in _AUTO_SKIP_METRICS
        ):
            continue
        good += label == "good"
        wrong += label == "wrong"
        by_rule[str(outcome.get("rule_id") or "")][label] += 1
    graded = good + wrong
    return {
        "real_graded": graded,
        "real_good": good,
        "real_accuracy": round(good / graded, 4) if graded else None,
        "by_rule": {rule: by_rule[rule] for rule in sorted(by_rule)},
        "stop_cost_mae_s": round(float(median(stop_errors)), 3) if stop_errors else None,
        "laps_of_pace_mae": round(float(median(pace_errors)), 3) if pace_errors else None,
    }


def build_scorecard(
    scenarios: Mapping[str, Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    checks_total = checks_passed = 0
    guards_total = guards_passed = targets_total = targets_passed = 0
    for scenario in scenarios.values():
        if scenario.get("result") == "skipped":
            continue
        checks = scenario.get("checks", [])
        checks_total += len(checks)
        checks_passed += sum(bool(check.get("ok")) for check in checks)
        if scenario.get("status") == "guard":
            guards_total += 1
            guards_passed += scenario.get("result") == "pass"
        elif scenario.get("status") == "target":
            targets_total += 1
            targets_passed += scenario.get("result") == "pass"
    rate = checks_passed / checks_total if checks_total else None
    metrics = {
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "check_pass_rate": rate,
        "score": round(100 * rate, 1) if rate is not None else None,
        "guards_total": guards_total,
        "guards_passed": guards_passed,
        "targets_total": targets_total,
        "targets_passed": targets_passed,
        **_real_metrics(outcomes),
    }
    return {
        "version": 1,
        "created_at": created_at or datetime.now(UTC).isoformat(),
        "scenarios": dict(scenarios),
        "metrics": metrics,
    }


def _scenario_map(scorecard: Mapping[str, Any]) -> Mapping[str, Any]:
    value = scorecard.get("scenarios", {})
    return value if isinstance(value, Mapping) else {}


def _check_signature(check: Mapping[str, Any]) -> tuple[str, tuple[str, ...], tuple[int, ...]]:
    return (
        str(check.get("type") or ""),
        tuple(str(rule) for rule in check.get("rules", [])),
        tuple(int(lap) for lap in check.get("laps", [])),
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _rule_accuracy(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    good = int(value.get("good", 0))
    wrong = int(value.get("wrong", 0))
    total = good + wrong
    return good / total if total else None


def compare(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    tolerance: float = 0.02,
) -> GateResult:
    if baseline is None:
        return GateResult("pass", [], [], ["no baseline"])

    failures: list[str] = []
    incomplete: set[str] = set()
    improvements: list[str] = []
    current_scenarios = _scenario_map(current)
    baseline_scenarios = _scenario_map(baseline)
    for scenario_id, scenario in current_scenarios.items():
        result = scenario.get("result")
        if scenario.get("status") == "guard" and result in ("fail", "error"):
            failures.append(f"guard scenario {scenario_id} {result}")
        if scenario.get("status") == "guard" and result == "skipped":
            incomplete.add(f"guard scenario {scenario_id} skipped")
        old = baseline_scenarios.get(scenario_id)
        if not isinstance(old, Mapping):
            continue
        old_result = old.get("result")
        if old_result == "pass" and result in ("fail", "error"):
            failures.append(f"scenario {scenario_id} regressed from pass to {result}")
        if old_result != "skipped" and result == "skipped":
            incomplete.add(f"scenario {scenario_id} skipped")
        old_checks = {
            _check_signature(check): check
            for check in old.get("checks", [])
            if isinstance(check, Mapping)
        }
        for check in scenario.get("checks", []):
            if not isinstance(check, Mapping):
                continue
            previous = old_checks.get(_check_signature(check))
            if previous is not None and previous.get("ok") and not check.get("ok"):
                signature = _check_signature(check)
                failures.append(
                    f"check regressed in {scenario_id}: {signature[0]} {list(signature[1])} "
                    f"laps {list(signature[2])}"
                )
    current_metrics = current.get("metrics", {})
    baseline_metrics = baseline.get("metrics", {})
    if not isinstance(current_metrics, Mapping):
        current_metrics = {}
    if not isinstance(baseline_metrics, Mapping):
        baseline_metrics = {}

    current_accuracy = _number(current_metrics.get("real_accuracy"))
    baseline_accuracy = _number(baseline_metrics.get("real_accuracy"))
    current_graded = int(current_metrics.get("real_graded", 0))
    baseline_graded = int(baseline_metrics.get("real_graded", 0))
    if (
        current_graded >= 5
        and baseline_graded >= 5
        and current_accuracy is not None
        and baseline_accuracy is not None
        and baseline_accuracy - current_accuracy > tolerance
    ):
        failures.append(
            f"real accuracy dropped from {baseline_accuracy:.4f} to {current_accuracy:.4f}"
        )

    current_by_rule = current_metrics.get("by_rule", {})
    baseline_by_rule = baseline_metrics.get("by_rule", {})
    if isinstance(current_by_rule, Mapping) and isinstance(baseline_by_rule, Mapping):
        for rule in sorted(set(current_by_rule) & set(baseline_by_rule)):
            current_counts = current_by_rule[rule]
            baseline_counts = baseline_by_rule[rule]
            if not isinstance(current_counts, Mapping) or not isinstance(baseline_counts, Mapping):
                continue
            current_n = int(current_counts.get("good", 0)) + int(current_counts.get("wrong", 0))
            baseline_n = int(baseline_counts.get("good", 0)) + int(baseline_counts.get("wrong", 0))
            current_rule_accuracy = _rule_accuracy(current_counts)
            baseline_rule_accuracy = _rule_accuracy(baseline_counts)
            if (
                current_n >= 5
                and baseline_n >= 5
                and current_rule_accuracy is not None
                and baseline_rule_accuracy is not None
                and baseline_rule_accuracy - current_rule_accuracy > 0.10
            ):
                failures.append(
                    f"rule accuracy dropped for {rule}: {baseline_rule_accuracy:.4f} "
                    f"to {current_rule_accuracy:.4f}"
                )

    for metric in ("stop_cost_mae_s", "laps_of_pace_mae"):
        current_value = _number(current_metrics.get(metric))
        baseline_value = _number(baseline_metrics.get(metric))
        if (
            current_value is not None
            and baseline_value is not None
            and baseline_value > 0
            and current_value > baseline_value * 1.10
        ):
            failures.append(f"{metric} rose from {baseline_value:.3f} to {current_value:.3f}")

    baseline_score = _number(baseline_metrics.get("score"))
    current_score = _number(current_metrics.get("score"))
    if baseline_score is not None and current_score is not None:
        improvements.append(
            f"score {baseline_score:.1f} -> {current_score:.1f} "
            f"({current_score - baseline_score:+.1f})"
        )
    for scenario_id in sorted(set(current_scenarios) & set(baseline_scenarios)):
        if (
            baseline_scenarios[scenario_id].get("result") == "fail"
            and current_scenarios[scenario_id].get("result") == "pass"
        ):
            improvements.append(f"scenario {scenario_id} fail -> pass")
    if baseline_accuracy is not None and current_accuracy is not None:
        improvements.append(
            f"real_accuracy {baseline_accuracy:.4f} -> {current_accuracy:.4f} "
            f"({current_accuracy - baseline_accuracy:+.4f})"
        )
    for metric in ("stop_cost_mae_s", "laps_of_pace_mae"):
        baseline_value = _number(baseline_metrics.get(metric))
        current_value = _number(current_metrics.get(metric))
        if baseline_value is not None and current_value is not None:
            improvements.append(
                f"{metric} {baseline_value:.3f} -> {current_value:.3f} "
                f"({current_value - baseline_value:+.3f})"
            )
    status: Literal["pass", "fail", "incomplete"] = (
        "fail" if failures else "incomplete" if incomplete else "pass"
    )
    return GateResult(status, failures, sorted(incomplete), improvements)


def trend(entries: Sequence[Mapping[str, Any]], window: int = 5) -> str:
    if window < 1:
        raise ValueError("window must be positive")
    if len(entries) < 2:
        return "not enough history"
    latest_score = float(entries[-1]["score"])
    previous = entries[max(0, len(entries) - 1 - window) : -1]
    if latest_score < max(float(entry["score"]) for entry in previous) - 0.5:
        return "degrading"
    recent = entries[-window:]
    recent_scores = [float(entry["score"]) for entry in recent]
    if (
        len(entries) >= window
        and max(recent_scores) - min(recent_scores) < 0.5
        and len({entry.get("targets_passed") for entry in recent}) == 1
    ):
        return "stagnant"
    if latest_score > float(recent[0]["score"]):
        return "improving"
    return "flat"
