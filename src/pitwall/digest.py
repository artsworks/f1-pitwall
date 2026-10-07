"""Session digest (docs/20 L1): the compressed, keep-forever memory of one
session. Every number is derived from SQLite (laps, stints, pit_events, calls,
plan_events, call_grades, bookmarks, outcomes), so a digest can always be
rebuilt and two digests can be diffed."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pitwall.config.thresholds import threshold as _th
from pitwall.derive import is_synthetic_uid
from pitwall.hindsight import Outcome, grade_and_store, stint_compound, stints, stop_laps
from pitwall.store.db import Database

DIGEST_VERSION = 3


def _record_inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("inputs")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _finite_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _repeat_call_ids(db: Database, uid: int) -> set[str]:
    calls = db.calls_for_session(uid)
    repeat_ids: set[str] = set()
    marked_replays: list[tuple[str, str, float]] = []
    legacy_fired: dict[str, list[tuple[float, str]]] = defaultdict(list)
    say_again: list[tuple[float, str, str]] = []

    for row in calls:
        outcome = row.get("outcome")
        call_id = row.get("call_id")
        rule_id = str(row.get("rule_id") or "")
        row_time = _finite_time(row.get("t"))
        if outcome == "fired":
            repeat_of = _record_inputs(row).get("repeat_of")
            if repeat_of is not None:
                if call_id is not None:
                    repeat_ids.add(str(call_id))
                if row_time is not None:
                    marked_replays.append((str(repeat_of), rule_id, row_time))
            elif call_id is not None and rule_id and row_time is not None:
                legacy_fired[rule_id].append((row_time, str(call_id)))
        elif outcome == "say_again" and call_id is not None and rule_id and row_time is not None:
            say_again.append((row_time, str(call_id), rule_id))

    claimed: set[str] = set()
    for press_time, original_id, rule_id in say_again:
        if any(
            target_id == original_id
            and replay_rule == rule_id
            and press_time <= replay_time <= press_time + 30
            for target_id, replay_rule, replay_time in marked_replays
        ):
            continue
        candidates = [
            (fired_time, call_id)
            for fired_time, call_id in legacy_fired.get(rule_id, [])
            if call_id not in claimed and press_time <= fired_time <= press_time + 30
        ]
        if candidates:
            _, replay_id = min(candidates)
            repeat_ids.add(replay_id)
            claimed.add(replay_id)
    return repeat_ids


def call_quality(db: Database, uid: int) -> dict[str, Any]:
    calls = db.calls_for_session(uid)
    repeat_ids = _repeat_call_ids(db, uid)
    fired_calls = []
    for row in calls:
        rule_id = str(row.get("rule_id") or "")
        call_id = row.get("call_id")
        if (
            row.get("outcome") == "fired"
            and rule_id != "reply"
            and not rule_id.startswith("menu:")
            and (call_id is None or str(call_id) not in repeat_ids)
        ):
            fired_calls.append(row)

    grades = {
        str(grade.get("call_id")): grade
        for grade in db.grades_for_session(uid)
        if grade.get("call_id") is not None
    }
    fired_ids = {
        str(call.get("call_id")) for call in fired_calls if call.get("call_id") is not None
    }
    fired_grades = {call_id: grades[call_id] for call_id in fired_ids if call_id in grades}
    fired = len(fired_calls)
    questions = [row for row in db.driver_inputs_for_session(uid) if row.get("kind") == "question"]
    unanswered = 0
    for row in questions:
        inputs = row.get("inputs")
        if isinstance(inputs, str):
            try:
                inputs = json.loads(inputs)
            except json.JSONDecodeError:
                inputs = {}
        case = inputs.get("case") if isinstance(inputs, dict) else None
        if case == "unknown" or not str(row.get("reply") or "").strip():
            unanswered += 1
    neg = sum(row.get("outcome") == "neg" for row in calls)
    return {
        "fired": fired,
        "graded": len(fired_grades),
        "ungraded": fired - len(fired_grades),
        "good": sum(grade.get("grade") == "good" for grade in fired_grades.values()),
        "good_pct": round(
            100 * sum(grade.get("grade") == "good" for grade in fired_grades.values()) / fired, 1
        )
        if fired
        else None,
        "neg": neg,
        "neg_rate_pct": round(100 * neg / fired, 1) if fired else None,
        "press_graded": sum(grade.get("source") == "press" for grade in fired_grades.values()),
        "questions": len(questions),
        "unanswered_questions": unanswered,
    }


def quality_trend(
    db: Database,
    sessions: int = 10,
    pack_dir: Path | None = None,
) -> dict[str, Any]:
    from pitwall.debrief import _session_label, _track_name

    db_sessions = db.sessions()
    db_uids = {int(session["uid"]) for session in db_sessions}
    db_sessions = [session for session in db_sessions if not is_synthetic_uid(int(session["uid"]))]
    rows = []
    structures: dict[tuple[int, int, str], tuple[int, ...]] = {}

    def structure_for(session: Mapping[str, Any]) -> tuple[int, ...]:
        key = (
            int(session.get("weekend_link") or 0),
        -1 if session.get("track_id") is None else int(session["track_id"]),
            str(session.get("weekend_structure") or ""),
        )
        if key not in structures:
            structures[key] = db.stored_weekend_structure(session)
        return structures[key]

    if sessions > 0:
        for session in reversed(db_sessions):
            quality = call_quality(db, int(session["uid"]))
            if not quality["fired"]:
                continue
            rows.append(
                _quality_session_row(
                    int(session["uid"]),
                    session.get("started_at"),
                    session.get("track_id"),
                    session.get("session_type"),
                    quality,
                    _track_name,
                    _session_label,
                    structure_for(session),
                )
            )
            if len(rows) >= sessions:
                break
    if sessions > 0 and pack_dir is not None:
        from pitwall.learnpack import LEDGER_NAME, read_ledger

        for uid, session in read_ledger(pack_dir / LEDGER_NAME).items():
            if is_synthetic_uid(uid) or uid in db_uids:
                continue
            ledger_quality = session.get("quality")
            if not isinstance(ledger_quality, dict):
                continue
            fired = ledger_quality.get("fired")
            if isinstance(fired, bool) or not isinstance(fired, int) or fired <= 0:
                continue
            rows.append(
                _quality_session_row(
                    uid,
                    session.get("started_at"),
                    session.get("track_id"),
                    session.get("session_type"),
                    ledger_quality,
                    _track_name,
                    _session_label,
                    structure_for(session),
                )
            )
    rows.sort(key=_quality_sort_key)
    rows = rows[-sessions:] if sessions > 0 else []

    trend = None
    if len(rows) > 1:
        midpoint = len(rows) // 2
        older = statistics.fmean(float(row["good_pct"]) for row in rows[:midpoint])
        newer = statistics.fmean(float(row["good_pct"]) for row in rows[midpoint:])
        trend = {
            "older_mean_good_pct": round(older, 1),
            "newer_mean_good_pct": round(newer, 1),
            "delta_pct": round(newer - older, 1),
            "sessions": len(rows),
        }
    return {"sessions": rows, "trend": trend}


def _quality_session_row(
    uid: int,
    started_at: object,
    track_id: object,
    session_type: object,
    quality: Mapping[str, Any],
    track_name: Callable[[Any], str],
    session_label: Callable[[Any, Sequence[int]], str],
    weekend_structure: Sequence[int] = (),
) -> dict[str, Any]:
    timestamp = _finite_time(started_at)
    return {
        "uid": uid,
        "started_at": started_at,
        "date": (
            time.strftime("%Y-%m-%d", time.localtime(timestamp))
            if timestamp is not None
            else "unknown"
        ),
        "track": track_name(track_id),
        "session_type": session_label(session_type, weekend_structure),
        **quality,
    }


def _quality_sort_key(row: Mapping[str, Any]) -> tuple[bool, float, int]:
    started_at = _finite_time(row.get("started_at"))
    return (
        started_at is None,
        started_at if started_at is not None else 0.0,
        int(row["uid"]),
    )


def startup_scorecard(db: Database, pack_dir: Path | None = None) -> str:
    if pack_dir is None:
        minutes = db.track_minutes()
    else:
        from pitwall.learnpack import pack_track_minutes

        minutes = pack_track_minutes(db, pack_dir)
    report = quality_trend(db, pack_dir=pack_dir) if pack_dir is not None else quality_trend(db)
    rows = report["sessions"]
    if not rows:
        quality_text = "n/a"
    else:
        good_pct = rows[-1]["good_pct"]
        quality_text = f"{good_pct:g}% good" if good_pct is not None else "n/a"
        trend = report["trend"]
        if trend is not None and good_pct is not None:
            quality_text += f" ({trend['delta_pct']:+.1f} over last {trend['sessions']})"
    return (
        f"pitwall: {round(minutes['minutes']):,} track minutes over "
        f"{minutes['sessions']:,} sessions · call quality {quality_text}"
    )


def _mean(xs: Sequence[float]) -> float | None:
    return round(statistics.fmean(xs), 2) if xs else None


def _errors(outcomes: Sequence[Outcome], metric: str) -> list[float]:
    return [o.error for o in outcomes if o.metric == metric and o.error is not None]


def findings(
    outcomes: Sequence[Outcome],
    calls: Mapping[str, Mapping[str, Any]],
    bookmarks: int,
    th: Mapping[str, object],
) -> list[str]:
    """Deterministic top findings, most costly first."""
    scored: list[tuple[float, str]] = []
    for o in outcomes:
        if o.metric == "stop_cost_s" and o.label == "wrong" and o.error is not None:
            scored.append((o.error, f"Stop cost {o.error:.1f}s: {o.detail}"))
        elif o.metric == "plan_followed" and o.label == "wrong":
            scored.append((2.0, f"Plan not followed from lap {o.lap}: {o.detail}"))
    lap_err = _errors(outcomes, "lap_ms")
    lap_bias = statistics.fmean(lap_err) if lap_err else 0.0
    if len(lap_err) >= 3 and abs(lap_bias) > _th(th, "hind_lap_tol_ms", 500):
        scored.append(
            (
                abs(lap_bias) / 1000,
                f"Lap predictions {lap_bias:+.0f} ms off on average ({len(lap_err)} calls)",
            )
        )
    fuel_err = _errors(outcomes, "fuel_margin")
    if fuel_err and abs(statistics.fmean(fuel_err)) > _th(th, "hind_fuel_tol_laps", 0.5):
        e = statistics.fmean(fuel_err)
        scored.append((abs(e), f"Fuel margin predicted {e:+.1f} laps vs the finish"))
    for rule_id, c in calls.items():
        wrong = int(c["auto_wrong"]) + int(c["human_bad"])
        judged = wrong + int(c["auto_good"]) + int(c["human_good"])
        if wrong >= 2 and wrong * 2 >= judged:
            scored.append((1.0 + wrong / 10, f"{rule_id}: {wrong} of {judged} judged calls wrong"))
        if int(c["neg"]) >= 2:
            scored.append((1.0, f"{rule_id}: driver said no {c['neg']} times"))
    ignored = [o for o in outcomes if o.label == "ignored"]
    if ignored:
        laps = ", ".join(f"L{o.lap}" for o in ignored)
        scored.append((1.5, f"{len(ignored)} box call(s) not taken: {laps}"))
    if bookmarks:
        scored.append((0.5, f"{bookmarks} bookmark(s) to review"))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [text for _, text in scored[: int(_th(th, "digest_max_findings", 5))]]


def build_digest(
    db: Database,
    uid: int,
    th: Mapping[str, object],
    *,
    setup_rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    outcomes = grade_and_store(db, uid, th, setup_rules=setup_rules)
    quality = call_quality(db, uid)
    laps = db.laps_for(uid, 0)
    stops = stop_laps(laps)
    parts = stints(laps, stops)
    session = db.session_row(uid) or {}
    green = [
        lap.lap_time_ms
        for lap in laps
        if lap.valid == 1 and lap.sc_status == 0 and lap.lap_time_ms > 0
    ]

    calls: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "fired": 0,
            "suppressed": 0,
            "ack": 0,
            "neg": 0,
            "human_good": 0,
            "human_bad": 0,
            "press_good": 0,
            "press_noise": 0,
            "auto_good": 0,
            "auto_wrong": 0,
        }
    )
    for c in db.calls_for_session(uid):
        outcome = str(c.get("outcome") or "")
        if outcome in ("fired", "suppressed", "ack", "neg"):
            calls[str(c.get("rule_id") or "")][outcome] += 1
    for g in db.grades_for_session(uid):
        source = str(g.get("source") or "human")
        if source == "press":
            key = "press_good" if g["grade"] == "good" else "press_noise"
        else:
            key = "human_good" if g["grade"] == "good" else "human_bad"
        calls[str(g["rule_id"])][key] += 1
    for o in outcomes:
        if o.label == "good":
            calls[o.rule_id]["auto_good"] += 1
        elif o.label == "wrong":
            calls[o.rule_id]["auto_wrong"] += 1
    calls.pop("", None)

    pits = db.pit_events_for_session(uid)
    bookmarks = db.bookmarks_for_session(uid)
    labels = Counter(o.label for o in outcomes)
    setup_recs = db.setup_recs_for_session(uid)
    setup_outcomes = [o for o in outcomes if o.metric.startswith("setup:")]
    setup_graded_ids = {o.call_id for o in setup_outcomes if o.label in {"good", "wrong"}}
    setup_counts = Counter(o.label for o in setup_outcomes)
    return {
        "version": DIGEST_VERSION,
        "session": {
            "uid": uid,
            "track_id": session.get("track_id"),
            "session_type": session.get("session_type"),
            "game_mode": session.get("game_mode"),
            "weather": session.get("weather"),
            "started_at": session.get("started_at"),
            "config_hash": session.get("config_hash"),
            "laps": laps[-1].lap_num if laps else 0,
        },
        "pace": {
            "valid_green_laps": len(green),
            "best_lap_ms": min(green) if green else None,
            "median_lap_ms": int(statistics.median(green)) if green else None,
            "stdev_ms": int(statistics.pstdev(green)) if len(green) > 1 else None,
        },
        "stints": [
            {
                "compound": stint_compound(p),
                "start_lap": p[0].lap_num,
                "end_lap": p[-1].lap_num,
                "start_age": p[0].tyre_age_laps,
            }
            for p in parts
        ],
        "stops": stops,
        "pit_events": [
            {"lap": p.lap_num, "loss_s": round(p.loss_ms / 1000, 2), "neutralised": p.neutralised}
            for p in pits
        ],
        "strategy": {
            "executed": "-".join(stint_compound(p) for p in parts),
            "plan_events": [
                {k: e.get(k) for k in ("lap", "kind", "from_plan", "to_plan", "reason", "sequence")}
                for e in db.plan_events_for_session(uid)
            ],
            "stop_cost_s": [o.error for o in outcomes if o.metric == "stop_cost_s"],
        },
        "model": {
            "lap_ms_error_mean": _mean(_errors(outcomes, "lap_ms")),
            "laps_of_pace_error_mean": _mean(_errors(outcomes, "laps_of_pace")),
            "fuel_margin_error_mean": _mean(_errors(outcomes, "fuel_margin")),
            "pit_loss_green_s": _mean([p.loss_ms / 1000 for p in pits if not p.neutralised]),
        },
        "outcomes": dict(sorted(labels.items())),
        "setup": {
            "recs": len(setup_recs),
            "graded": setup_counts["good"] + setup_counts["wrong"],
            "good": setup_counts["good"],
            "wrong": setup_counts["wrong"],
            "ignored": setup_counts["ignored"],
            "censored": setup_counts["censored"],
            "na": setup_counts["n/a"],
            "open_experiments": sum(
                rec.get("evidence", {}).get("tier") == "experiment"
                and rec.get("rec_id") not in setup_graded_ids
                for rec in setup_recs
                if isinstance(rec.get("evidence"), dict)
            ),
        },
        "calls": dict(sorted(calls.items())),
        "quality": quality,
        "bookmarks": [
            {"lap": b.get("lap"), "kind": b.get("kind") or "hold", "note": b.get("note") or ""}
            for b in bookmarks
        ],
        "findings": findings(outcomes, calls, len(bookmarks), th),
    }


def format_digest(d: Mapping[str, Any]) -> str:
    s = d["session"]
    quality = d.get("quality", {})
    good_pct = quality.get("good_pct")
    neg_pct = quality.get("neg_rate_pct")
    good_text = f"{good_pct:g}%" if good_pct is not None else "n/a"
    neg_text = f"{neg_pct:g}%" if neg_pct is not None else "n/a"
    lines = [
        f"session {s['uid']} track {s['track_id']} laps {s['laps']}"
        f"  strategy {d['strategy']['executed'] or '-'}  stops {d['stops']}",
        f"outcomes {d['outcomes']}",
        f"quality {good_text} good · neg {neg_text} · "
        f"{quality.get('unanswered_questions', 0)} unanswered questions · "
        f"{quality.get('ungraded', 0)} ungraded",
        "findings:",
    ]
    lines += [f"  - {f}" for f in d["findings"]] or ["  (none)"]
    return "\n".join(lines)
