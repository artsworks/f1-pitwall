"""Setup recommendation grading against later runs on the same track."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import Any

from pitwall.hindsight import CENSORED, GOOD, IGNORED, NA, WRONG, Outcome
from pitwall.rules.expr import TrackedNamespace
from pitwall.setup.rules import SetupRules, setup_fields_for_param
from pitwall.setup.signals import RunSignals, signals_for_run, slip_base_for_session
from pitwall.setup.states import Run, runs_for_session
from pitwall.store.db import Database, _uid_from_sql, _uid_to_sql

_OBJECTIVE_FLOORS = {
    "w_deg": 20.0,
    "w_lim": 0.1,
    "w_th": 0.3,
    "w_ev": 1.0,
}


def _threshold(th: Mapping[str, Any], name: str, default: float) -> float:
    value = th.get(name, default)
    return float(value) if isinstance(value, int | float) else default


def _green_count(run: Run) -> int:
    return sum(bool(lap.valid) and lap.sc_status == 0 for lap in run.laps)


def _rel(before: float, after: float, floor: float) -> float:
    return (after - before) / max(abs(before), floor)


def _sum_available(values: Sequence[float | None]) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _symptom_metric(rule_id: str, signals: RunSignals) -> float | None:
    if rule_id == "entry_instability":
        return _sum_available((signals.lockups_rear_per10, signals.snaps_entry_per10))
    if rule_id == "traction_limited":
        return _sum_available((signals.traction_exits_per10, signals.snaps_exit_per10))
    if rule_id == "rear_wear_limited":
        return signals.wear_axle_ratio
    if rule_id == "understeer_balance":
        return signals.slip_raw
    if rule_id == "oversteer_balance":
        return -signals.slip_raw if signals.slip_raw is not None else None
    return None


def _objective_value(signals: RunSignals, name: str) -> float | None:
    if name == "w_deg":
        return signals.lap_slope_ms
    if name == "w_lim":
        if signals.wear_front_slope is None or signals.wear_rear_slope is None:
            return None
        return max(signals.wear_front_slope, signals.wear_rear_slope)
    if name == "w_th":
        if signals.z_front is None or signals.z_rear is None:
            return None
        return abs(signals.z_front) + abs(signals.z_rear)
    if name == "w_ev":
        return _sum_available(
            (
                signals.traction_exits_per10,
                signals.lockups_rear_per10,
                signals.lockups_front_per10,
                signals.snaps_entry_per10,
                signals.snaps_exit_per10,
            )
        )
    return None


def _objective_rel(before: RunSignals, after: RunSignals, rules: SetupRules) -> float:
    weighted: list[tuple[float, float]] = []
    for name, weight in rules.objective.items():
        left = _objective_value(before, name)
        right = _objective_value(after, name)
        if left is None or right is None or weight <= 0:
            continue
        weighted.append((weight, _rel(left, right, _OBJECTIVE_FLOORS[name])))
    total_weight = sum(weight for weight, _ in weighted)
    if total_weight <= 0:
        return 0.0
    return sum(weight * value for weight, value in weighted) / total_weight


def _predicate_true(predicate: Any, signals: RunSignals, th: Mapping[str, Any]) -> bool:
    environment = {item.name: getattr(signals, item.name) for item in fields(RunSignals)}
    environment.update(th)
    try:
        return bool(predicate(TrackedNamespace(environment)))
    except (TypeError, KeyError, AttributeError):
        return False


def _new_contra(
    rule_id: str, before: RunSignals, after: RunSignals, th: Mapping[str, Any], rules: SetupRules
) -> bool:
    symptom = next((item for item in rules.symptoms if item.rule_id == rule_id), None)
    if symptom is None:
        return False
    return any(
        not _predicate_true(predicate, before, th) and _predicate_true(predicate, after, th)
        for predicate in symptom.contra
    )


def _param_value(fields_map: Mapping[str, Any], param: str, rules: SetupRules) -> float | None:
    names = setup_fields_for_param(param, rules)
    if not names:
        return None
    values = [fields_map.get(name) for name in names]
    numeric_values = [float(value) for value in values if isinstance(value, int | float)]
    if len(numeric_values) != len(values):
        return None
    if param in {"front_pressure", "rear_pressure"}:
        return sum(numeric_values) / len(numeric_values)
    return numeric_values[0]


def _same_value(left: object, right: object) -> bool:
    if isinstance(left, int | float) and isinstance(right, int | float):
        return abs(float(left) - float(right)) <= 1e-6
    return left == right


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    changed: set[str] = set()
    for name in set(before) | set(after):
        if name == "fuel_load":
            continue
        if name not in before or name not in after or not _same_value(before[name], after[name]):
            changed.add(name)
    return changed


def _details(
    *,
    param: str,
    delta: float,
    applied_delta: float | None,
    applied_sign: int,
    before_session: int,
    after_session: int | None,
    before_state: int | None,
    after_state: int | None,
    laps_before: int,
    laps_after: int,
    j_rel: float | None,
    status: str,
) -> str:
    return json.dumps(
        {
            "param": param,
            "delta": delta,
            "applied_delta": applied_delta,
            "applied_sign": applied_sign,
            "before_session": before_session,
            "after_session": after_session,
            "before_state": before_state,
            "after_state": after_state,
            "laps_before": laps_before,
            "laps_after": laps_after,
            "J_rel": j_rel,
            "status": status,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _session_runs(db: Database, sessions: Sequence[Mapping[str, Any]]) -> dict[int, list[Run]]:
    return {int(session["uid"]): runs_for_session(db, int(session["uid"])) for session in sessions}


def _before_run(runs: Sequence[Run], setup_state_id: int | None, lap: int) -> Run | None:
    matching = [run for run in runs if run.setup_state_id == setup_state_id and run.end_lap <= lap]
    if matching:
        return max(matching, key=lambda run: run.end_lap)
    return next((run for run in runs if run.start_lap <= lap <= run.end_lap), None)


def _after_run(
    sessions: Sequence[Mapping[str, Any]],
    runs_by_session: Mapping[int, Sequence[Run]],
    session_index: int,
    lap: int,
    before_run: Run | None,
    compound: int,
    minimum_laps: int,
) -> tuple[int, Run] | None:
    for index in range(session_index, len(sessions)):
        candidate_uid = int(sessions[index]["uid"])
        for run in runs_by_session.get(candidate_uid, ()):
            if index == session_index:
                if run.start_lap <= lap or (
                    before_run is not None and run.start_lap <= before_run.end_lap
                ):
                    continue
            if run.compound != compound or _green_count(run) < minimum_laps:
                continue
            return candidate_uid, run
    return None


def _outcome(
    *,
    db: Database,
    rec: Mapping[str, Any],
    source_uid: int,
    target_uid: int,
    before_run: Run | None,
    before_signals: RunSignals | None,
    after_run: Run,
    after_signals: RunSignals,
    th: Mapping[str, Any],
    rules: SetupRules,
) -> Outcome:
    rule_id = str(rec.get("rule_id") or "")
    symptom = rule_id
    param = str(rec.get("param") or "")
    delta = float(rec.get("delta") or 0.0)
    tolerance = _threshold(th, "setup_grade_tol", 0.03)
    before_fields = (
        db.setup_state_fields(before_run.setup_state_id) or {}
        if before_run is not None and before_run.setup_state_id is not None
        else {}
    )
    after_fields = (
        db.setup_state_fields(after_run.setup_state_id) or {}
        if after_run.setup_state_id is not None
        else {}
    )
    changed = _changed_fields(before_fields, after_fields)
    param_fields = set(setup_fields_for_param(param, rules))
    other_changes = changed - param_fields
    before_value = _param_value(before_fields, param, rules)
    after_value = _param_value(after_fields, param, rules)
    applied_delta = (
        after_value - before_value if before_value is not None and after_value is not None else None
    )
    applied_sign = (
        0
        if applied_delta is None or abs(applied_delta) <= 1e-6
        else (1 if applied_delta > 0 else -1)
    )
    symptom_before = _symptom_metric(symptom, before_signals) if before_signals else None
    symptom_after = _symptom_metric(symptom, after_signals)
    metric_floor = {
        "entry_instability": 1.0,
        "traction_limited": 1.0,
        "rear_wear_limited": 0.01,
        "understeer_balance": 0.5,
        "oversteer_balance": 0.5,
    }.get(symptom, 1.0)
    actual = (
        _rel(symptom_before, symptom_after, metric_floor)
        if symptom_before is not None and symptom_after is not None
        else None
    )
    j_rel = (
        _objective_rel(before_signals, after_signals, rules) if before_signals is not None else None
    )
    contra = bool(
        before_signals is not None
        and _new_contra(symptom, before_signals, after_signals, th, rules)
    )
    status = ""
    if before_value is None or after_value is None:
        label = NA
        status = "missing_setup_state"
    elif applied_sign == 0:
        label = IGNORED
        status = "unchanged"
    elif other_changes:
        label = NA
        status = "confounded"
    elif applied_sign != (1 if delta > 0 else -1):
        label = IGNORED
        status = "opposite"
    elif actual is None:
        label = NA
        status = "missing_metric"
    elif actual >= tolerance or contra or (j_rel is not None and j_rel >= tolerance):
        label = WRONG
    elif actual <= -tolerance and (j_rel is None or j_rel < tolerance) and not contra:
        label = GOOD
    else:
        label = NA
    detail = _details(
        param=param,
        delta=delta,
        applied_delta=applied_delta,
        applied_sign=applied_sign,
        before_session=source_uid,
        after_session=target_uid,
        before_state=before_run.setup_state_id if before_run else None,
        after_state=after_run.setup_state_id,
        laps_before=_green_count(before_run) if before_run else 0,
        laps_after=_green_count(after_run),
        j_rel=j_rel,
        status=status,
    )
    return Outcome(
        call_id=str(rec["rec_id"]),
        rule_id=f"setup.{symptom}",
        lap=int(rec.get("lap") or 0),
        metric=f"setup:{symptom}",
        predicted=-tolerance,
        actual=actual,
        error=actual + tolerance if actual is not None else None,
        label=label,
        detail=detail,
    )


def grade_setup_recs(
    db: Database,
    uid: int,
    th: Mapping[str, Any],
    rules: SetupRules,
) -> list[Outcome]:
    """Grade setup advice whose first qualifying after-run belongs to `uid`."""
    uid = _uid_from_sql(_uid_to_sql(uid))
    session = db.session_row(uid)
    if session is None:
        return []
    track_id = int(session["track_id"])
    sessions = db.sessions_for_track(track_id)
    session_index = next(
        (index for index, item in enumerate(sessions) if int(item["uid"]) == uid),
        None,
    )
    if session_index is None:
        return []
    runs_by_session = _session_runs(db, sessions)
    recs: list[tuple[int, dict[str, Any]]] = []
    for item in sessions[: session_index + 1]:
        source_uid = int(item["uid"])
        recs.extend(
            (source_uid, rec)
            for rec in db.setup_recs_for_session(source_uid)
            if int(rec.get("track_id") or track_id) == track_id
        )
    deduplicated: dict[tuple[int, str, str], tuple[int, dict[str, Any]]] = {}
    for source_uid, rec in recs:
        key = (source_uid, str(rec.get("rule_id") or ""), str(rec.get("param") or ""))
        if key not in deduplicated or int(rec.get("lap") or 0) < int(
            deduplicated[key][1].get("lap") or 0
        ):
            deduplicated[key] = (source_uid, rec)

    minimum_laps = int(_threshold(th, "setup_grade_min_laps", 5))
    outcomes: list[Outcome] = []
    for (source_uid, _, _), (_, rec) in deduplicated.items():
        source_index = next(
            (index for index, item in enumerate(sessions) if int(item["uid"]) == source_uid),
            None,
        )
        if source_index is None:
            continue
        evidence = rec.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        tier = evidence.get("tier")
        if tier == "next_visit" and source_index >= len(sessions) - 1:
            continue
        lap = int(rec.get("lap") or 0)
        source_runs = runs_by_session.get(source_uid, ())
        before_run = _before_run(source_runs, rec.get("setup_state_id"), lap)
        after = _after_run(
            sessions,
            runs_by_session,
            source_index,
            lap,
            before_run,
            int(rec.get("compound") or 0),
            minimum_laps,
        )
        if after is None:
            if source_uid != uid:
                continue
            detail = _details(
                param=str(rec.get("param") or ""),
                delta=float(rec.get("delta") or 0.0),
                applied_delta=None,
                applied_sign=0,
                before_session=source_uid,
                after_session=None,
                before_state=before_run.setup_state_id if before_run else None,
                after_state=None,
                laps_before=_green_count(before_run) if before_run else 0,
                laps_after=0,
                j_rel=None,
                status="censored",
            )
            outcomes.append(
                Outcome(
                    call_id=str(rec["rec_id"]),
                    rule_id=f"setup.{rec.get('rule_id') or ''}",
                    lap=lap,
                    metric=f"setup:{rec.get('rule_id') or ''}",
                    predicted=-_threshold(th, "setup_grade_tol", 0.03),
                    actual=None,
                    error=None,
                    label=CENSORED,
                    detail=detail,
                )
            )
            continue
        after_uid, after_run = after
        if after_uid != uid:
            continue
        before_signals = (
            signals_for_run(
                db,
                source_uid,
                before_run,
                th,
                learned_slip_base=slip_base_for_session(db, source_uid, before_run.compound, th),
            )
            if before_run is not None
            else None
        )
        after_signals = signals_for_run(
            db,
            after_uid,
            after_run,
            th,
            learned_slip_base=slip_base_for_session(db, after_uid, after_run.compound, th),
        )
        outcomes.append(
            _outcome(
                db=db,
                rec=rec,
                source_uid=source_uid,
                target_uid=after_uid,
                before_run=before_run,
                before_signals=before_signals,
                after_run=after_run,
                after_signals=after_signals,
                th=th,
                rules=rules,
            )
        )
    return outcomes
