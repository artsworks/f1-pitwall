"""Learn setup gains and slip baselines from graded runs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pitwall.setup.rules import SetupRules
from pitwall.setup.signals import signals_for_run
from pitwall.setup.states import runs_for_session
from pitwall.store.db import Database


def learned_gains(
    db: Database, track_id: int, compound: int
) -> dict[tuple[str, str, int], tuple[float, float]]:
    gains: dict[tuple[str, str, int], tuple[float, float]] = {}
    for param in db.params_for_track(track_id):
        if param.compound != compound or not param.name.startswith("setup_gain:"):
            continue
        parts = param.name.split(":")
        if len(parts) != 4 or parts[3] not in {"+", "-"}:
            continue
        gains[(parts[1], parts[2], 1 if parts[3] == "+" else -1)] = (
            param.value,
            param.weight,
        )
    return gains


def fold_setup_learning(
    db: Database,
    uid: int,
    outcomes: Sequence[Any],
    th: Mapping[str, Any],
    *,
    rules: SetupRules | None = None,
) -> None:
    if rules is None:
        from pitwall.hindsight import _setup_rules

        rules = _setup_rules(None)
    from pitwall.hindsight import GOOD, IGNORED, NA, WRONG

    minimum_run_laps = float(th.get("setup_min_run_laps", 6))
    minimum_event_laps = int(th.get("setup_min_event_laps", 3))
    handled: set[str] = set()
    with db.transaction():
        for outcome in outcomes:
            if not str(outcome.metric).startswith("setup:"):
                continue
            try:
                detail = json.loads(str(outcome.detail or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(detail, dict):
                continue
            status = str(detail.get("status") or "")
            opposite = outcome.label == IGNORED and status == "opposite"
            if outcome.label not in {GOOD, WRONG, NA} and not opposite:
                continue
            if status == "confounded" or outcome.actual is None:
                continue
            rec_id = str(outcome.call_id)
            if rec_id in handled:
                continue
            rec = db.setup_rec_by_id(rec_id)
            if rec is None or int(rec.get("folded") or 0):
                continue
            applied_sign = int(detail.get("applied_sign") or 0)
            param = str(detail.get("param") or rec.get("param") or "")
            symptom = str(outcome.metric).removeprefix("setup:")
            spec = rules.params.get(param)
            applied_delta = detail.get("applied_delta")
            laps_before = int(detail.get("laps_before") or 0)
            laps_after = int(detail.get("laps_after") or 0)
            if (
                applied_sign not in {-1, 1}
                or spec is None
                or not isinstance(applied_delta, int | float)
            ):
                continue
            weight = min(laps_before, laps_after) / minimum_run_laps
            if weight <= 0:
                continue
            steps = max(1.0, abs(float(applied_delta)) / spec.step)
            gain_name = f"setup_gain:{symptom}:{param}:{'+' if applied_sign > 0 else '-'}"
            if db.mark_setup_rec_folded(rec_id):
                db.fold_param(
                    int(rec["track_id"]),
                    int(rec["compound"]),
                    gain_name,
                    value=-float(outcome.actual) / steps,
                    weight=weight,
                )
                handled.add(rec_id)

        session = db.session_row(uid)
        if session is None or int(session.get("setup_folded") or 0):
            return
        baseline_folded = False
        for run in runs_for_session(db, uid):
            green_laps = sum(bool(lap.valid) and lap.sc_status == 0 for lap in run.laps)
            if green_laps < minimum_event_laps:
                continue
            signals = signals_for_run(db, uid, run, th)
            if signals.slip_raw is None:
                continue
            db.fold_param(
                int(session["track_id"]),
                run.compound,
                "setup_base:slip_balance_deg",
                value=signals.slip_raw,
                weight=green_laps / minimum_run_laps,
            )
            baseline_folded = True
        if baseline_folded:
            db.mark_setup_session_folded(uid)
