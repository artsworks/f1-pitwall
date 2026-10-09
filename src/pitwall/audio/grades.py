"""Replay radio scheduling grades against the packaged rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import yaml

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import PolicySettings, RuleDefModel, Settings
from pitwall.rules.engine import Candidate, Rule, RuleEngine
from pitwall.state.session import Snapshot


@dataclass(slots=True)
class Scenario:
    id: str
    data: dict[str, Any]


@dataclass(slots=True)
class ScenarioResult:
    id: str
    order: list[str]
    actions: dict[str, str]
    mismatches: list[tuple[str, str, str]]


def load_workbook(path: Path) -> list[Scenario]:
    workbook = yaml.safe_load(path.read_text())
    if not isinstance(workbook, dict) or workbook.get("kind") != "radio-grades":
        raise ValueError(f"{path}: expected kind: radio-grades")
    defaults = workbook.get("defaults", {}).get("policy", {})
    scenarios = []
    for batch in workbook.get("batches", []):
        for raw in batch.get("scenarios", []):
            data = dict(raw)
            data["policy"] = {**defaults, **data.get("policy", {})}
            scenarios.append(Scenario(data["id"], data))
    return scenarios


def _merged(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


def _snapshot(now: float, scene: dict[str, Any], session: str) -> Snapshot:
    return Snapshot(**{"now": now, "session_kind": session, **scene})


def _candidate(
    engine: RuleEngine,
    call: dict[str, Any],
    snap: Snapshot,
) -> Candidate:
    rule_id = call["rule"]
    if call.get("hypothetical"):
        definition = RuleDefModel(
            id=rule_id,
            priority=call.get("priority", 2),
            urgency=call.get("kind", "info"),
            when="True",
            say=call.get("text", ""),
        )
        engine = RuleEngine(
            [*(item.defn for item in engine.rules), definition],
            thresholds=engine.thresholds,
            mode=engine.mode,
        )
    candidate = engine.candidate_for(rule_id, snap, call.get("text"))
    if call.get("kind") and call["kind"] != "reply":
        definition = candidate.rule.defn.model_copy(update={"urgency": call["kind"]})
        candidate.rule = Rule(definition)
        candidate.tags = list(definition.tags)
    stale = bool(call.get("stale"))
    if stale:
        candidate.still_true = lambda _: False
        candidate.current = lambda _: False
    elif call.get("scene") is None:
        candidate.still_true = None
        candidate.current = None
    candidate.inputs["grade_id"] = call["id"]
    return candidate


def replay(
    scenario: Scenario,
    settings: Settings | None = None,
    *,
    session_uid: int | None = None,
) -> ScenarioResult:
    settings = settings or ConfigStore(isolated=True).current()
    data = scenario.data
    policy_data = _merged(settings.policy.model_dump(), data.get("policy", {}))
    policy = PolicySettings.model_validate(policy_data)
    thresholds = _merged(settings.thresholds, data.get("thresholds", {}))
    scene = dict(data.get("scene", {}))
    if session_uid is not None:
        scene["session_uid"] = session_uid
    session = data.get("session", "unknown")
    now = float(scene.get("now", 100.0))
    start_snapshot = _snapshot(now, scene, session)
    engine = RuleEngine(settings.rules, thresholds=thresholds, mode={})
    output = StringIO()
    dispatcher = Dispatcher(policy, VirtualClock(now), DecisionLog(fp=output), sinks=[])
    dispatcher.reset_session(start_snapshot.session_uid)
    call_to_grade: dict[str, str] = {}
    initial_id: str | None = None
    current = data.get("currently_speaking")
    if current:
        current_reply: Call | None = None
        start = now - float(current.get("age_s", 0.5))
        current_scene = {**scene, **current.get("scene", {})}
        snap = _snapshot(start, current_scene, session)
        if current.get("kind") == "reply":
            menu_items = {item.id: item for item in settings.menu.items}
            item = menu_items.get(current.get("menu_item"))
            dispatcher.menu_reply(
                current.get("text", ""),
                current.get("rule", "menu_reply"),
                snap,
                item.related_rules if item else [],
            )
            current_reply = next(
                (
                    item.call
                    for item in dispatcher._queue
                    if item.call.rule_id == current.get("rule")
                ),
                None,
            )
            if current_reply is not None:
                current_reply.inputs["grade_id"] = current["id"]
                call_to_grade[current_reply.id] = current["id"]
        else:
            dispatcher.submit([_candidate(engine, current, snap)], snap)
            on_air = next(
                (
                    item.call
                    for item in dispatcher._queue
                    if item.call.inputs.get("grade_id") == current["id"]
                ),
                None,
            )
            if on_air is not None:
                call_to_grade[on_air.id] = current["id"]
                initial_id = on_air.id
        if initial_id is None and current.get("kind") == "reply":
            initial_id = current_reply.id if current_reply is not None else None
        spoken = dispatcher.drain(start)
        if spoken:
            initial_id = spoken[-1].id
            call_to_grade[initial_id] = current["id"]
        dispatcher.submit([], start_snapshot)
    queued_calls = sorted(data.get("queued", []), key=lambda call: -float(call.get("age_s", 0)))
    for call in queued_calls:
        submit_time = now - float(call.get("age_s", 0))
        call_scene = {**scene, **call.get("scene", {})}
        snap = _snapshot(submit_time, call_scene, session)
        if call.get("kind") == "reply":
            menu_items = {item.id: item for item in settings.menu.items}
            item = menu_items.get(call.get("menu_item"))
            dispatcher.menu_reply(
                call.get("text", ""),
                call.get("rule", "menu_reply"),
                snap,
                item.related_rules if item else [],
            )
            for queued in dispatcher._queue:
                if queued.call.rule_id == call.get("rule") and queued.call.t == submit_time:
                    queued.call.inputs["grade_id"] = call["id"]
                    call_to_grade[queued.call.id] = call["id"]
                    break
        else:
            candidate = _candidate(engine, call, snap)
            before = {item.call.id for item in dispatcher._queue}
            dispatcher.submit([candidate], snap)
            for queued in dispatcher._queue:
                if queued.call.id not in before:
                    queued.call.inputs["grade_id"] = call["id"]
                    call_to_grade[queued.call.id] = call["id"]
                    if call.get("not_before_s") is not None:
                        queued.call.not_before = now + float(call["not_before_s"])
        if call.get("not_before_s") is not None:
            for queued in dispatcher._queue:
                if queued.call.inputs.get("grade_id") == call["id"]:
                    queued.call.not_before = now + float(call["not_before_s"])
    dispatcher.submit([], start_snapshot)

    emitted: list[Call] = []
    t = now
    while t <= now + 60:
        emitted.extend(dispatcher.drain(t))
        if not dispatcher._queue:
            break
        t += 0.5
    logs = [json.loads(line) for line in output.getvalue().splitlines()]
    outcomes: dict[str, str] = {}
    actions: dict[str, str] = {}
    for record in logs:
        grade_id = record.get("inputs", {}).get("grade_id")
        if grade_id:
            outcomes[grade_id] = record.get("outcome", "")
    for record in logs:
        inputs = record.get("inputs", {})
        grade_id = inputs.get("grade_id")
        if grade_id and record.get("outcome") == "merged":
            call_to_grade[record.get("call_id")] = grade_id
        if grade_id and record.get("outcome") == "digested":
            actions[grade_id] = "merge"
        if grade_id and record.get("outcome") == "digest_overflow":
            actions[grade_id] = "drop"
    speaker_cut = bool(
        current
        and any(
            record.get("call_id") == initial_id
            and record.get("outcome") in {"suppressed", "requeued"}
            and record.get("suppressed_by") == "preempted"
            for record in logs
        )
    )
    current_requeued = bool(
        current
        and any(
            record.get("call_id") == initial_id and record.get("outcome") == "requeued"
            for record in logs
        )
    )
    order: list[str] = []
    for call in emitted:
        grade_id = call.inputs.get("grade_id")
        if call.rule_id == "digest":
            order.append("digest")
            for source_id in call.merged_ids:
                source_grade = call_to_grade.get(source_id)
                if source_grade:
                    actions[source_grade] = "merge"
            continue
        if grade_id is None:
            continue
        if not current or grade_id != current.get("id") or speaker_cut:
            order.append(grade_id)
        if current and grade_id == current.get("id"):
            actions[grade_id] = "queue"
        else:
            index = len([item for item in order if item != "digest"]) - 1
            if index == 0:
                actions[grade_id] = "now" if speaker_cut or initial_id is None else "next"
            elif index == 1:
                actions[grade_id] = "next"
            else:
                actions[grade_id] = "queue"
    if current:
        actions[current["id"]] = (
            "queue"
            if current_requeued and current["id"] in order
            else "drop"
            if speaker_cut
            else "now"
        )
    for call_id in data.get("grade", {}):
        default_action = "merge" if outcomes.get(call_id) in {"merged", "digested"} else "drop"
        actions.setdefault(call_id, default_action)

    mismatches: list[tuple[str, str, str]] = []
    expected_grades = dict(data.get("grade", {}))
    for group in data.get("rotate", []):
        winner = next((call_id for call_id in group if call_id in order), None)
        if winner is not None:
            for call_id in group:
                expected_grades[call_id] = (
                    data.get("grade", {}).get(winner, "now") if call_id == winner else "drop"
                )
    for call_id, expected in expected_grades.items():
        if call_id not in actions or actions[call_id] != expected:
            mismatches.append((call_id, expected, actions.get(call_id, "missing")))
    expected_order = data.get("order", [])
    order_matches = len(order) == len(expected_order) and all(
        got in expected.split("|") for got, expected in zip(order, expected_order, strict=True)
    )
    if not order_matches:
        mismatches.append(("order", str(expected_order), str(order)))
    for call_id, substring in data.get("expect_text", {}).items():
        spoken_text = next(
            (call.text for call in emitted if call.inputs.get("grade_id") == call_id), ""
        )
        if substring.casefold() not in spoken_text.casefold():
            mismatches.append((call_id, substring, spoken_text))
    return ScenarioResult(scenario.id, order, actions, mismatches)


def format_table(results: list[ScenarioResult]) -> str:
    rows = ["| id | call | expected | got |", "| --- | --- | --- | --- |"]
    rows.extend(
        f"| {result.id} | {call} | {expected} | {got} |"
        for result in results
        for call, expected, got in result.mismatches
    )
    return "\n".join(rows)
