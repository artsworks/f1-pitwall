"""Deterministic, standalone post-session review built from persisted evidence."""

from __future__ import annotations

import html
import json
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from pitwall.config.models import Settings
from pitwall.learned import learned_state
from pitwall.setup.evaluate import evaluate, explain
from pitwall.setup.rules import parse_setup_rules
from pitwall.setup.signals import session_signals
from pitwall.state.session import thermal_window
from pitwall.store.db import Database, LapRow, StintRow

_STYLE = (
    "<style>:root{color-scheme:dark}body{background:#111821;color:#e4eaf2;"
    "font:16px/1.5 system-ui;max-width:1000px;margin:auto;padding:1.5rem}"
    "a{color:#94d9cf}section{border-top:1px solid #445064;padding:1rem 0}"
    "article{border-left:3px solid #3fbf9c;padding:.1rem 1rem;margin:.8rem 0}"
    ".scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}"
    "td,th{border-bottom:1px solid #445064;padding:.55rem;text-align:left}"
    "svg{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
    "small{color:#abb8c8}details{margin:1rem 0}</style>"
)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _cell(value: object) -> str:
    if isinstance(value, float):
        value = f"{value:.1f}"
    return f"<td>{_esc(value)}</td>"


def _table(headers: tuple[str, ...], rows: Sequence[tuple[object, ...]], source: str) -> str:
    head = "".join(f"<th scope='col'>{_esc(label)}</th>" for label in headers)
    body = "".join("<tr>" + "".join(_cell(value) for value in row) + "</tr>" for row in rows)
    return (
        f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
        f"</table></div><small>Source: {_esc(source)}.</small>"
    )


def _plot(laps: list[LapRow], stints: list[StintRow]) -> str:
    timed = [lap for lap in laps if lap.lap_time_ms > 0]
    if not timed:
        return "<p>No lap times recorded.</p>"
    low = min(lap.lap_time_ms for lap in timed)
    high = max(lap.lap_time_ms for lap in timed)
    first = min(lap.lap_num for lap in timed)
    last = max(lap.lap_num for lap in timed)
    marks = []
    curves = []
    for stint in stints:
        if stint.n_valid_laps < 2:
            continue
        x1 = 32 + 730 * (stint.start_lap - first) / max(1, last - first)
        x2 = 32 + 730 * (stint.end_lap - first) / max(1, last - first)
        start_ms = stint.base_ms
        end_ms = stint.base_ms + stint.deg_ms_per_lap * (stint.end_lap - stint.start_lap)
        y1 = 172 - 140 * (start_ms - low) / max(1, high - low)
        y2 = 172 - 140 * (end_ms - low) / max(1, high - low)
        curves.append(
            f"<path d='M{x1:.1f} {y1:.1f}L{x2:.1f} {y2:.1f}'"
            " stroke='#ffc67a' stroke-width='2' fill='none'>"
            f"<title>Compound {stint.compound}: fitted degradation "
            f"{stint.deg_ms_per_lap:.1f} ms/lap</title></path>"
        )
    for lap in timed:
        x = 32 + 730 * (lap.lap_num - first) / max(1, last - first)
        y = 172 - 140 * (lap.lap_time_ms - low) / max(1, high - low)
        color = "#3fbf9c" if lap.valid and lap.sc_status == 0 else "#b5b9c3"
        marks.append(
            f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{color}'>"
            f"<title>Lap {lap.lap_num}: {lap.lap_time_ms / 1000:.1f}s, "
            f"{'valid' if lap.valid else 'invalid'}</title></circle>"
        )
    return (
        "<svg role='img' aria-label='Lap times by lap; green valid, grey invalid, "
        "amber fitted stint degradation' "
        "viewBox='0 0 800 205' preserveAspectRatio='xMidYMid meet'>"
        "<path d='M32 32V172H762' fill='none' stroke='#8791a5'/>"
        + "".join(curves)
        + "".join(marks)
        + f"<text x='32' y='195' fill='#bec7d2'>Lap {first}</text>"
        + f"<text x='690' y='195' fill='#bec7d2'>Lap {last}</text></svg>"
    )


def _section(section_id: str, title: str, content: str) -> str:
    return f"<section id='{section_id}'><h2>{_esc(title)}</h2>{content}</section>"


def _setup_number(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value:g}"
    return str(value)


def _setup_actions(db: Database, uid: int, session: dict[str, Any], settings: Settings) -> str:
    stored = [row for row in db.setup_recs_for_session(uid) if row.get("mode") == "debrief"]
    rows: list[tuple[object, ...]] = []
    locked: set[tuple[str, str]] = set()
    event_laps = 0
    source = "setup_recs, laps, setup_states"
    if stored:
        for rec in stored:
            evidence = rec.get("evidence", {})
            signals = evidence.get("signals", {}) if isinstance(evidence, dict) else {}
            if isinstance(signals, dict):
                event_laps = max(event_laps, int(signals.get("event_laps", 0) or 0))
            from_value = float(rec.get("from_value") or 0)
            to_value = evidence.get("to_value", from_value + float(rec.get("delta") or 0))
            rows.append(
                (
                    evidence.get("tier", ""),
                    rec.get("param", ""),
                    f"{_setup_number(from_value)} → {_setup_number(to_value)}",
                    rec.get("conf", ""),
                    evidence.get("expect", ""),
                    evidence.get("tradeoff", ""),
                    ", ".join(
                        f"{key}={value}"
                        for key, value in sorted(signals.items())
                        if key not in {"run_laps", "compound", "setup_state_id"}
                    )
                    if isinstance(signals, dict)
                    else "",
                )
            )
            suppressions = evidence.get("suppressed", []) if isinstance(evidence, dict) else []
            if isinstance(suppressions, list):
                locked.update(
                    (str(item.get("param", "")), str(rec.get("rule_id", "")))
                    for item in suppressions
                    if isinstance(item, dict) and item.get("reason") == "locked"
                )
    else:
        signals = session_signals(db, uid, settings.thresholds)
        if signals is not None:
            event_laps = signals.event_laps
            setup = (
                db.setup_state_fields(signals.setup_state_id)
                if signals.setup_state_id is not None
                else None
            )
            if setup is None:
                changes = db.setup_changes_for_session(uid)
                if changes:
                    setup = db.setup_state_fields(int(changes[-1]["to_state"]))
            setup = setup or {}
            parc_ferme_value = session.get("parc_ferme")
            parc_ferme = int(parc_ferme_value) if parc_ferme_value is not None else -1
            rules = parse_setup_rules(settings.setup_rules)
            recommendations = evaluate(
                signals,
                setup,
                mode="debrief",
                parc_ferme=parc_ferme,
                rules=rules,
                thresholds=settings.thresholds,
            )
            suppressions = explain(
                signals,
                setup,
                mode="debrief",
                parc_ferme=parc_ferme,
                rules=rules,
                thresholds=settings.thresholds,
            )
            locked.update(
                (item.get("param", ""), item.get("rule_id", ""))
                for item in suppressions
                if item.get("reason") == "locked"
            )
            for recommendation in recommendations:
                event_laps = max(
                    event_laps,
                    int(recommendation.evidence.get("event_laps", 0) or 0),
                )
                rows.append(
                    (
                        recommendation.tier,
                        recommendation.param,
                        f"{_setup_number(recommendation.from_value)} "
                        f"→ {_setup_number(recommendation.to_value)}",
                        recommendation.conf,
                        recommendation.expect,
                        recommendation.tradeoff,
                        ", ".join(
                            f"{key}={value}"
                            for key, value in sorted(recommendation.evidence.items())
                            if key not in {"run_laps", "compound", "setup_state_id"}
                        ),
                    )
                )
                locked.update(
                    (item.get("param", ""), recommendation.rule_id)
                    for item in recommendation.suppressed
                    if item.get("reason") == "locked"
                )

    content = (
        _table(
            (
                "Tier",
                "Parameter",
                "Current → proposed",
                "Confidence",
                "Why",
                "Trade-off",
                "Evidence",
            ),
            rows,
            source,
        )
        if rows
        else (
            "<p>No setup change suggested: no symptom passed its threshold. "
            f"Run event laps: {event_laps}.</p><small>Source: {_esc(source)}.</small>"
        )
    )
    locked_html = "".join(
        f"<p>Would suggest {_esc(param)} ({_esc(rule)}), locked by parc fermé.</p>"
        for param, rule in sorted(locked)
        if param and rule
    )
    return content + locked_html


def render_debrief(db: Database, uid: int, settings: Settings, *, editable: bool = False) -> str:
    session = db.session_row(uid)
    if session is None:
        raise ValueError(f"session {uid} not found")
    laps = db.laps_for(uid)
    stints = db.stints_for_session(uid)
    pits = db.pit_events_for_session(uid)
    calls = db.calls_for_session(uid)
    grades = {str(row["call_id"]): row for row in db.grades_for_session(uid)}
    outcomes: dict[str, list[dict[str, Any]]] = {}
    for row in db.outcomes_for_session(uid):
        outcomes.setdefault(str(row["call_id"]), []).append(row)
    inputs = db.driver_inputs_for_session(uid)
    valid = [lap for lap in laps if lap.valid and lap.sc_status == 0 and lap.lap_time_ms > 0]
    fired = [call for call in calls if call["outcome"] == "fired"]
    judged = Counter(
        str(grades.get(str(call["call_id"]), {}).get("grade") or "ungraded") for call in fired
    )
    mean = f"{statistics.fmean(lap.lap_time_ms for lap in valid) / 1000:.1f}s" if valid else "—"
    spread = f"{statistics.pstdev(lap.lap_time_ms for lap in valid) / 1000:.1f}s" if valid else "—"
    summary = (
        f"<p>{len(laps)} laps · {len(valid)} clean green laps · {len(pits)} stops · "
        f"{len(fired)} calls · {judged['good']} graded good · {judged['wrong']} graded wrong. "
        f"Mean clean pace {mean}, spread {spread}.</p>"
        + _table(
            ("Track", "Session", "Calls mode", "Recording"),
            [
                (
                    session["track_id"] if session["track_id"] is not None else "—",
                    session.get("session_type") or "—",
                    session.get("calls_mode") or "unknown",
                    session.get("recording_path") or "not linked",
                )
            ],
            "sessions",
        )
    )
    sections = [_section("summary", "00 Summary", summary)]
    sections.append(
        _section(
            "pace",
            "01 Pace and stints",
            _plot(laps, stints)
            + _table(
                ("Lap", "Time (s)", "Compound", "Age", "Fuel (kg)", "Validity"),
                [
                    (
                        lap.lap_num,
                        f"{lap.lap_time_ms / 1000:.1f}",
                        lap.compound,
                        lap.tyre_age_laps,
                        f"{lap.fuel_kg:.1f}",
                        "valid"
                        if lap.valid and not lap.sc_status
                        else ", ".join(lap.invalid_reasons) or "SC",
                    )
                    for lap in laps
                ],
                "laps; grey plot markers are invalid or neutralised laps",
            )
            + _table(
                ("Compound", "Laps", "Fitted deg (ms/lap)", "Fit laps", "RMSE (ms)"),
                [
                    (
                        stint.compound,
                        f"{stint.start_lap}–{stint.end_lap}",
                        f"{stint.deg_ms_per_lap:.1f}",
                        stint.n_valid_laps,
                        f"{stint.rmse_ms:.1f}",
                    )
                    for stint in stints
                ],
                "stints; fit values only where sufficient valid laps exist",
            ),
        )
    )
    sectors = [
        (
            label,
            f"{statistics.fmean(values) / 1000:.1f}" if values else "—",
            len(values),
        )
        for label, values in (
            ("S1", [lap.s1_ms for lap in valid if lap.s1_ms > 0]),
            ("S2", [lap.s2_ms for lap in valid if lap.s2_ms > 0]),
            (
                "S3",
                [
                    lap.lap_time_ms - lap.s1_ms - lap.s2_ms
                    for lap in valid
                    if lap.s1_ms > 0 and lap.s2_ms > 0
                ],
            ),
        )
    ]
    sections.append(
        _section(
            "sectors",
            "02 Sectors",
            _table(
                ("Sector", "Mean (s)", "Clean laps"),
                sectors,
                "laps.s1_ms, s2_ms, lap_time_ms",
            ),
        )
    )
    by_compound: dict[int, list[LapRow]] = {}
    for lap in valid:
        if lap.tyre_inner_c:
            by_compound.setdefault(lap.compound, []).append(lap)
    thermal_rows = []
    for compound, group in sorted(by_compound.items()):
        cold, hot = thermal_window(settings.thresholds, compound)
        green = sum(cold <= lap.tyre_inner_c <= hot for lap in group)
        thermal_rows.append((compound, f"{green / len(group):.0%}", len(group), cold, hot))
    sections.append(
        _section(
            "tyres",
            "03 Tyres",
            _table(
                ("Compound", "In window", "Measured laps", "Cold below (°C)", "Hot above (°C)"),
                thermal_rows,
                "laps.tyre_inner_c and configured thresholds; lap means",
            ),
        )
    )
    call_rows = []
    strategy_rows = []
    for call in calls:
        call_id = str(call.get("call_id") or "")
        human = grades.get(call_id)
        automatic = outcomes.get(call_id, [])
        verdict = (
            str(human["grade"])
            if human
            else (", ".join(sorted({str(item["label"]) for item in automatic})) or "ungraded")
        )
        raw_inputs = call.get("inputs")
        try:
            parsed_inputs = json.loads(raw_inputs) if raw_inputs else {}
        except json.JSONDecodeError:
            parsed_inputs = {"raw": raw_inputs}
        detail = json.dumps(parsed_inputs, sort_keys=True, indent=2, default=str)
        evidence = json.dumps(automatic, sort_keys=True, default=str)
        call_rows.append(
            "<article><h3>Lap "
            + _esc(call.get("lap") or "—")
            + " · "
            + _esc(call.get("rule_id") or "—")
            + " · "
            + _esc(verdict)
            + "</h3><p>"
            + _esc(call.get("text") or "")
            + "</p>"
            + "<p>Decision: "
            + _esc(call["outcome"])
            + (
                " · Suppressed by " + _esc(call["suppressed_by"])
                if call.get("suppressed_by")
                else ""
            )
            + "</p><details><summary>Exact inputs and hindsight</summary><pre>"
            + _esc(detail)
            + "</pre><pre>"
            + _esc(evidence)
            + "</pre></details>"
            + (
                "<p>Grade: "
                + "".join(
                    f"<button type='button' data-call='{_esc(call_id)}' "
                    f"data-grade='{grade}'>{grade}</button> "
                    for grade in ("good", "noise", "too_late", "wrong")
                )
                + "</p>"
                if editable and call_id
                else ""
            )
            + "</article>"
        )
        if call["outcome"] == "fired" and any(
            word in str(call.get("rule_id") or "")
            for word in ("pit", "box", "stop", "undercut", "extend", "plan")
        ):
            strategy_rows.append(
                (call.get("lap"), call.get("rule_id"), call.get("text"), verdict, evidence)
            )
    sections.append(
        _section(
            "strategy",
            "04 Strategy calls",
            _table(
                ("Lap", "Rule", "Called", "Verdict", "Hindsight"),
                strategy_rows,
                "calls, call_grades, outcomes",
            )
            + _table(
                ("Pit lap", "Loss (s)", "Neutralised"),
                [(pit.lap_num, f"{pit.loss_ms / 1000:.1f}", bool(pit.neutralised)) for pit in pits],
                "pit_events",
            ),
        )
    )
    sections.append(
        _section(
            "radio",
            "05 Radio and decisions",
            ("".join(call_rows) or "<p>No calls recorded.</p>")
            + _table(
                ("Lap", "Input", "Rule"),
                [(item.get("lap"), item.get("action"), item.get("rule_id")) for item in inputs],
                "driver_inputs",
            ),
        )
    )
    sections.append(
        _section(
            "incidents",
            "06 Incidents and energy",
            _table(
                ("Lap", "ERS deployed (J)", "Fuel (kg)", "SC status"),
                [
                    (lap.lap_num, f"{lap.ers_deployed_j:.0f}", f"{lap.fuel_kg:.1f}", lap.sc_status)
                    for lap in laps
                ],
                "laps; raw event details require the recording index",
            ),
        )
    )
    learned = learned_state(
        db, settings, int(session["track_id"] if session["track_id"] is not None else -1)
    )
    findings = [
        f"Review {_esc(rule)}: {count} negative grades."
        for rule, count in sorted(
            Counter(
                str(grade["rule_id"])
                for grade in grades.values()
                if grade["grade"] in ("noise", "wrong", "too_late")
            ).items()
        )
        if count > 0
    ]
    sections.append(
        _section(
            "actions",
            "07 Actions and learned state",
            "<h3>Setup</h3>"
            + _setup_actions(db, uid, session, settings)
            + "<ul>"
            + "".join(f"<li>{text}</li>" for text in findings)
            + "</ul>"
            + "<details><summary>Persisted learning</summary><pre>"
            + _esc(json.dumps(learned, indent=2, default=str))
            + "</pre></details>"
            + "<small>Source: call_grades, model_params, sessions.</small>",
        )
    )
    recording = str(session.get("recording_path") or "")
    grade_script = (
        "<script>document.addEventListener('click',async e=>{"
        "const b=e.target.closest('button[data-grade]');if(!b)return;"
        f"const r=await fetch('/api/debrief/{uid}/grade',{{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({call_id:b.dataset.call,grade:b.dataset.grade})});"
        "if(r.ok)location.reload();else alert('Grade was not saved');});</script>"
        if editable
        else ""
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Session {_esc(uid)} · Pitwall debrief</title>"
        f"{_STYLE}</head><body>"
        f"<header><h1>Session {_esc(uid)} debrief</h1>"
        f"<p>Track {_esc(session.get('track_id') or '—')} · "
        f"{_esc(recording or 'No recording linked')}</p>"
        "</header><nav aria-label='Sections'>"
        + " · ".join(
            f"<a href='#{name}'>{_esc(name.title())}</a>"
            for name in (
                "summary",
                "pace",
                "sectors",
                "tyres",
                "strategy",
                "radio",
                "incidents",
                "actions",
            )
        )
        + "</nav>"
        + "".join(sections)
        + grade_script
        + "</body></html>"
    )


def render_debrief_index(db: Database, *, limit: int = 100) -> str:
    """HTML list of stored sessions, newest first, each linking to its debrief."""
    rows = db.sessions()[::-1][:limit]
    body = "".join(
        "<tr>"
        f"<td><a href='/debrief/{int(row['uid'])}'>{_esc(row['uid'])}</a></td>"
        + _cell(
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
            if row.get("started_at")
            else "—"
        )
        + _cell(row["track_id"] if row.get("track_id") is not None else "—")
        + _cell(row.get("session_type") if row.get("session_type") is not None else "—")
        + _cell(len(db.laps_for(int(row["uid"]))))
        + _cell(row.get("recording_path") or "not linked")
        + "</tr>"
        for row in rows
    )
    head = "".join(
        f"<th scope='col'>{label}</th>"
        for label in ("Session", "Start", "Track", "Type", "Laps", "Recording")
    )
    table = (
        f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
        "</table></div>"
        if rows
        else "<p>No sessions stored yet.</p>"
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Sessions · Pitwall debrief</title>{_STYLE}</head><body>"
        "<header><h1>Sessions</h1><p>Newest first. "
        "<a href='/debrief/latest'>Open the latest debrief</a>.</p></header>"
        f"{table}</body></html>"
    )
