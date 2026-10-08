from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from pitwall.cli.common import _resolve_recordings, emit_json_or, ingest_paths, open_db
from pitwall.config.loader import ConfigStore
from pitwall.input.menu import shortcut_warnings, validate_related_rules, validate_shortcuts
from pitwall.input.menu import validate as validate_menu
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import SessionState


def cmd_diff(args: argparse.Namespace) -> int:
    import glob as _glob

    from pitwall.diff import format_diff, run_diff

    settings = ConfigStore().current()
    recordings = [Path(f) for f in _resolve_recordings(args.recordings, settings)]
    if args.corpus:
        recordings += [Path(p) for p in sorted(_glob.glob(args.corpus))]
    if not recordings:
        print("diff: no recordings matched")
        return 0
    a_dir = Path(args.a) if args.a else None
    result = run_diff(
        recordings,
        a_dir,
        Path(args.b),
        a_mindset=args.a_mindset,
        b_mindset=args.b_mindset,
    )
    emit_json_or(args, result, format_diff)
    if args.record:
        from pitwall.store.db import open_configured
        from pitwall.tune import record_diff

        db = open_configured(settings)
        if db is not None:
            n = record_diff(
                db,
                result,
                a_dir=str(args.a or ""),
                b_dir=str(args.b),
                a_mindset=str(args.a_mindset or ""),
                b_mindset=str(args.b_mindset or ""),
            )
            print(f"diff: recorded {n} A/B rows")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Fold graded calls + A/B results from SQLite into persisted rule tuning."""
    from pitwall.tune import format_tune, tune_from_db

    settings = ConfigStore().current()
    db = open_db(args, settings, "tune")
    if db is None:
        return 1
    if args.paths:
        ingest_paths(args, db, settings)
    print(format_tune(tune_from_db(db, settings.thresholds)))
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """Grade a session in hindsight and write its compact JSON digest."""
    from pitwall.digest import build_digest, format_digest

    settings = ConfigStore().current()
    db = open_db(args, settings, "digest")
    if db is None:
        return 1
    if args.paths:
        out_dir = None if args.out in (None, "-") else Path(args.out)

        def show_ingest_result(result: Any) -> None:
            print(f"session {result.session_uid}: {result.status} ({result.path})")
            for finding in result.findings:
                print(f"  - {finding}")
            if result.error:
                print(f"  error: {result.error}")

        errors = ingest_paths(args, db, settings, out_dir, show=show_ingest_result)
        return 1 if errors else 0
    uid = db.latest_session_uid() if args.session in (None, "latest") else int(args.session)
    if uid is None:
        print("digest: no sessions in the database")
        return 1
    digest = build_digest(db, uid, settings.thresholds, setup_rules=settings.setup_rules)
    emit_json_or(args, digest, format_digest)
    if args.out != "-":
        default = Path(settings.persistence.path).expanduser().parent / "digests"
        out_dir = Path(args.out) if args.out else default
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{uid}.json"
        path.write_text(json.dumps(digest, indent=2, default=str))
        print(f"digest: {path}")
    return 0


def cmd_debrief(args: argparse.Namespace) -> int:
    from pitwall.debrief import render_debrief

    settings = ConfigStore().current()
    db = open_db(args, settings, "debrief")
    if db is None:
        return 1
    uid = db.latest_session_uid() if args.session == "latest" else int(args.session)
    if uid is None or db.session_row(uid) is None:
        print("debrief: session not found")
        return 1
    path = Path(args.out) if args.out else Path(f"debrief-{uid}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_debrief(db, uid, settings))
    print(f"debrief: {path}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from pitwall.setup.advisor import recommend_for_session
    from pitwall.setup.evaluate import explain

    settings = ConfigStore().current()
    db = open_db(args, settings, "setup")
    if db is None:
        return 1
    uid = db.latest_session_with_laps_uid() if args.session is None else int(args.session)
    if uid is None:
        print("setup: no session with player laps")
        return 1
    session = db.session_row(uid)
    if session is None:
        print(f"setup: session {uid} not found")
        return 1
    parc_ferme = int(session["parc_ferme"]) if session.get("parc_ferme") is not None else -1
    advice = recommend_for_session(
        db,
        uid,
        settings,
        args.mode,
        run_choice="longest" if args.mode == "debrief" else "latest",
        parc_ferme=parc_ferme,
        lap=None,
        store=args.store,
    )
    if advice is None:
        print(f"setup: session {uid} has no player laps")
        return 1
    recommendations = advice.recommendations

    def format_setup(_result: Any) -> None:
        print("tier | parameter | current → proposed | confidence | rule | evidence | suppressed")
        for rec in recommendations:
            evidence = ", ".join(f"{key}={value}" for key, value in list(rec.evidence.items())[:4])
            suppressed_text = ", ".join(
                f"{item['param']}:{item['reason']}" for item in rec.suppressed
            )
            print(
                f"{rec.tier} | {rec.param} | {rec.from_value:g} → {rec.to_value:g} | "
                f"{rec.conf} | {rec.rule_id} | {evidence} | {suppressed_text}"
            )
        if not recommendations:
            print("No setup recommendations.")
        suppression_explanations = explain(
            advice.signals,
            advice.setup,
            mode=args.mode,
            parc_ferme=parc_ferme,
            rules=advice.rules,
            thresholds=settings.thresholds,
            learned=advice.learned,
        )

        if suppression_explanations:
            items = ", ".join(
                f"{item.get('rule_id', item.get('param', 'candidate'))}:{item['reason']}"
                for item in suppression_explanations
            )
            print(f"Suppressed: {items}")

    emit_json_or(args, [asdict(rec) for rec in recommendations], format_setup)
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from pitwall.evaluate import evaluate_corpus

    db = open_db(args, ConfigStore().current(), "evaluate")
    if db is None:
        return 1
    result = evaluate_corpus(db, args.track)

    def format_evaluation(result: Any) -> None:
        for track in result["tracks"]:
            mode_status = "comparable" if track["comparable"] else "one mode only"
            print(f"Track {track['track_id']}: {mode_status}")
            for mode, row in (("on", track["on"]), ("off", track["off"])):
                print(
                    f"  calls {mode}: {row['sessions']} sessions, {row['completed_laps']} laps, "
                    f"clean pace p25/p50/p75={row['lap_time_s']['p25']}/"
                    f"{row['lap_time_s']['p50']}/{row['lap_time_s']['p75']} s; "
                    f"invalid laps={row['invalid_laps']} ({row['mistake_rate']}); "
                    f"negative call grades={row['negative_call_grades']}"
                )
        print(result["note"])

    emit_json_or(args, result, format_evaluation)
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    import yaml

    from pitwall.propose import propose_thresholds

    settings = ConfigStore().current()
    db = open_db(args, settings, "propose")
    if db is None:
        return 1
    result = propose_thresholds(db, settings, args.candidate_rules)
    output = yaml.safe_dump(result, sort_keys=False)
    if args.out:
        Path(args.out).write_text(output)
        print(f"propose: review {args.out} before editing YAML")
    else:
        print(output)
    for candidate in result.get("question_candidates", []):
        print(
            f"question candidate {candidate['label']} ({candidate['item_id']}): "
            f"{candidate['uncovered']} uncovered asks across {candidate['sessions']} sessions"
        )
        print(f"  median signals at ask: {json.dumps(candidate['at_ask'], sort_keys=True)}")
        for rule in candidate["rules"]:
            print(
                f"  {rule['rule_id']} thresholds: {json.dumps(rule['thresholds'], sort_keys=True)}"
            )
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from pitwall.calibrate import calibrate, format_calibration, write_overlays

    settings = ConfigStore().current()
    db = open_db(args, settings, "calibrate")
    if db is None:
        return 1
    errors = False
    if args.paths:

        def show_ingest_error(result: Any) -> None:
            if result.status == "error":
                print(f"ingest error {result.path}: {result.error}")

        errors = ingest_paths(args, db, settings, show=show_ingest_error) > 0
    report = calibrate(
        db,
        settings,
        track_id=args.track,
        dry_run=args.dry_run,
        include_synthetic=args.include_synthetic,
    )
    if args.write_overlay and not args.dry_run:
        report["overlays"] = [
            str(path) for path in write_overlays(report, settings, args.overlay_dir)
        ]

    def format_calibration_text(_report: Any) -> None:
        print(format_calibration(report))
        for path in report.get("overlays", []):
            print(f"overlay: {path}")

    emit_json_or(args, report, format_calibration_text)
    return 1 if errors else 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """List stored sessions newest first, with their debrief link."""
    from pitwall.derive import is_synthetic_uid

    settings = ConfigStore().current()
    db = open_db(args, settings, "sessions")
    if db is None:
        return 1
    rows = db.sessions()[::-1][: args.limit]
    if not rows:
        print("sessions: none in the database")
        return 0
    print(
        f"{'session_uid':<20} {'start':<16} {'track':>5} {'type':>4} {'laps':>4} "
        "syn  derived_from  recording"
    )
    for row in rows:
        uid = int(row["uid"])
        started = row.get("started_at")
        start = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
        rec = Path(row["recording_path"]).name if row.get("recording_path") else "-"
        synthetic = "yes" if row.get("synthetic") or is_synthetic_uid(uid) else ""
        derived_from = str(row.get("derived_from") or "")
        laps = len(db.laps_for(uid))
        print(
            f"{uid:<20} {start:<16} {row.get('track_id', '?'):>5} "
            f"{row.get('session_type', '?'):>4} {laps:>4}  {synthetic:<3} "
            f"{derived_from:<20} {rec}"
        )
    print("debrief: /debrief on the dashboard, or pitwall debrief --session <uid>")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from pitwall.learnpack import restore_pack

    settings = ConfigStore().current()
    db = open_db(args, settings, "restore")
    if db is None:
        return 1
    counts = restore_pack(
        db,
        args.pack,
        Path(settings.learning.pack_dir).expanduser(),
        args.overlay_dir,
    )
    summary = " ".join(f"{name}={count}" for name, count in counts.items())
    print(f"restore: {summary}")
    return 0


def cmd_rules_check(args: argparse.Namespace) -> int:
    """Validate all rule expressions compile and evaluate on a dummy snapshot."""
    store = ConfigStore()
    settings = store.current()
    engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=store.current().resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )
    menu_errors = (
        validate_menu(settings.menu)
        + validate_shortcuts(settings.input, settings.menu)
        + validate_related_rules(settings.menu, {rule.id for rule in settings.rules})
    )
    for err in menu_errors:
        print(f"rules check FAILED: {err}")
    for warning in shortcut_warnings(settings.input, settings.menu):
        print(f"rules check WARNING: {warning}")
    if menu_errors:
        return 1
    snap = SessionState().snapshot(0.0)
    try:
        result = engine.evaluate(snap)
    except Exception as e:
        print(f"rules check FAILED: {e}")
        return 1
    print(
        f"rules check: {len(engine.rules)} rules compiled, "
        f"{len(result.candidates)} candidates on empty snapshot"
    )
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from pitwall.bench import (
        build_recording_index,
        build_scorecard,
        compare,
        find_source,
        load_scenarios,
        run_scenario,
    )

    scenarios_dir = Path(args.scenarios).expanduser()
    if args.accept_changes and not args.update_baseline:
        print("bench: --accept-changes requires --update-baseline")
        return 1
    if args.trend:
        return _bench_history(scenarios_dir, args.window)
    if args.update_baseline and args.note is None:
        print("bench: --update-baseline requires --note")
        return 1
    if args.update_baseline and args.only:
        print("bench: --update-baseline cannot be used with --only")
        return 1

    rules_dir = Path(args.rules).expanduser() if args.rules else None
    try:
        all_scenarios = load_scenarios(scenarios_dir)
        scenario_ids = {scenario.id for scenario in all_scenarios}
        scenarios = load_scenarios(scenarios_dir, args.only)
        settings = ConfigStore(rules_dir=rules_dir, isolated=True).current()
        if args.recordings:
            recording_dirs = [Path(directory).expanduser() for directory in args.recordings]
        else:
            recording_dirs = [Path(ConfigStore().current().recording.directory).expanduser()]
        index = build_recording_index(recording_dirs)
        scenario_rows: dict[str, dict[str, Any]] = {}
        outcomes: list[dict[str, Any]] = []
        for scenario in scenarios:
            source, reason = find_source(scenario, index)
            row, scenario_outcomes = run_scenario(
                scenario,
                source,
                settings,
                rules_dir=rules_dir,
                skip_reason=reason,
            )
            scenario_rows[scenario.id] = row
            outcomes.extend(scenario_outcomes)
        scorecard = build_scorecard(scenario_rows, outcomes)
        baseline_path = (
            Path(args.baseline).expanduser() if args.baseline else scenarios_dir / "baseline.json"
        )
        baseline: dict[str, Any] | None = None
        if baseline_path.exists():
            loaded = json.loads(baseline_path.read_text())
            if not isinstance(loaded, dict):
                raise ValueError(f"{baseline_path}: baseline must be a JSON object")
            baseline = loaded
        gate = compare(scorecard, baseline, args.tolerance, scenario_ids=scenario_ids)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"bench: {exc}")
        return 1

    update_allowed = (
        not gate.failures and not gate.incomplete and (not gate.changed or args.accept_changes)
    )
    gate_data = {
        "status": gate.status,
        "failures": gate.failures,
        "incomplete": gate.incomplete,
        "improvements": gate.improvements,
        "changed": gate.changed,
        "accepted_changes": (gate.changed if args.update_baseline and update_allowed else []),
    }
    try:
        if args.out:
            output = Path(args.out).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"bench: {exc}")
        return 1
    if args.json:
        print(json.dumps({"scorecard": scorecard, "gate": gate_data}, indent=2, sort_keys=True))
    else:
        for scenario_id, scenario in scorecard["scenarios"].items():
            failed = [check for check in scenario["checks"] if not check["ok"]]
            line = f"{scenario_id} {scenario['status']} {scenario['result']}"
            if scenario["reason"]:
                line += f": {scenario['reason']}"
            if failed:
                details = [
                    f"{check['type']} {','.join(check['rules'])} "
                    f"laps {check['laps'][0]}-{check['laps'][1]} found {check['found']}"
                    for check in failed
                ]
                line += f" | failed: {'; '.join(details)}"
            print(line)
        metrics = scorecard["metrics"]
        pass_rate = (
            "n/a" if metrics["check_pass_rate"] is None else f"{metrics['check_pass_rate']:.1%}"
        )
        score = "n/a" if metrics["score"] is None else f"{metrics['score']:.1f}"
        print(
            f"checks {metrics['checks_passed']}/{metrics['checks_total']} "
            f"({pass_rate}), score {score}"
        )
        print(
            f"guards {metrics['guards_passed']}/{metrics['guards_total']}, "
            f"targets {metrics['targets_passed']}/{metrics['targets_total']}"
        )
        print(
            f"real accuracy {metrics['real_accuracy']}, good {metrics['real_good']}, "
            f"graded {metrics['real_graded']}, "
            f"stop cost MAE {metrics['stop_cost_mae_s']}, "
            f"pace MAE {metrics['laps_of_pace_mae']}"
        )
        for rule, counts in metrics["by_rule"].items():
            print(f"rule {rule}: {counts['good']} good, {counts['wrong']} wrong")
        print(f"gate: {gate.status}")
        for failure in gate.failures:
            print(f"failure: {failure}")
        for incomplete in gate.incomplete:
            print(f"incomplete: {incomplete}")
        for changed in gate.changed:
            print(f"changed: {changed}")
        if args.update_baseline and update_allowed:
            for changed in gate.changed:
                print(f"accepted: {changed}")
        for improvement in gate.improvements:
            print(f"improvement: {improvement}")

    if args.update_baseline:
        if not update_allowed:
            if (
                gate.changed
                and not args.accept_changes
                and not gate.failures
                and not gate.incomplete
            ):
                print(
                    "bench: baseline not updated, changed items need --accept-changes "
                    "after the expectation change is approved"
                )
            else:
                print(f"bench: baseline not updated, gate is {gate.status}")
        else:
            try:
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                baseline_path.write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n")
                metrics = scorecard["metrics"]
                history_entry = {
                    "date": datetime.now(UTC).date().isoformat(),
                    "note": args.note,
                    "score": metrics["score"],
                    "check_pass_rate": metrics["check_pass_rate"],
                    "guards": f"{metrics['guards_passed']}/{metrics['guards_total']}",
                    "targets": f"{metrics['targets_passed']}/{metrics['targets_total']}",
                    "targets_passed": metrics["targets_passed"],
                    "real_accuracy": metrics["real_accuracy"],
                    "real_graded": metrics["real_graded"],
                }
                history_path = scenarios_dir / "history.jsonl"
                history_path.parent.mkdir(parents=True, exist_ok=True)
                with history_path.open("a") as stream:
                    stream.write(json.dumps(history_entry, sort_keys=True) + "\n")
            except OSError as exc:
                print(f"bench: {exc}")
                return 1
    return {"pass": 0, "fail": 1, "incomplete": 2}[gate.status]


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _bench_history(scenarios_dir: Path, window: int) -> int:
    from pitwall.bench import trend

    history = scenarios_dir / "history.jsonl"
    try:
        lines = history.read_text().splitlines() if history.exists() else []
        entries = [json.loads(line) for line in lines if line.strip()]
        history_lines = [line for line in lines if line.strip()]
        for line in history_lines[-window:]:
            print(line)
        print(f"trend: {trend(entries, window)}")
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        print(f"bench: {exc}")
        return 1
    return 0
