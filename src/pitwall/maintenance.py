"""Automatic learned-state upkeep, run on every `pitwall start` and after each
session: nobody should need `pitwall digest` or SQLite to keep priors sane.

- quarantine: model_params rows that fail a sanity check (unknown track,
  slope at the fit clamp, implausible lap time) move to
  model_params_quarantine and stop feeding priors;
- rebuild (once per LEARN_VERSION): each stored stint is refit from its laps
  with today's model, then stint-derived priors are recomputed with today's
  gate and race-distance scoping;
- grade: sessions without hindsight outcomes are graded;
- calibrate and tune (`learning.auto_calibrate`): track priors are refit from
  stored laps and rule cooldowns from human grades plus auto-graded outcomes.
  Writes that would fail the quarantine check are dropped. Threshold YAML is
  never written; `pitwall propose` stays review-only. A watchdog restart
  mid-session passes `refit=False` so priors do not move during a race.

Deterministic and idempotent: a second run changes nothing."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from pitwall.calibrate import calibrate
from pitwall.config.models import Settings
from pitwall.hindsight import grade_and_store
from pitwall.model.deg import DEG_FUEL_REF, DegFit, fit_is_clean, fit_stint, scoped
from pitwall.protocol.enums import session_kind
from pitwall.setup.states import majority_state
from pitwall.store.db import Database, ModelParam
from pitwall.tune import (
    AB_PREFIX,
    COOLDOWN_PREFIX,
    TUNE_COMPOUND,
    TUNE_TRACK,
    load_cooldown_mults,
    tune_from_db,
)

LEARN_VERSION = 2
STINT_PARAMS = ("deg_ms_per_lap", "base_ms", "fuel_ms_per_lap", DEG_FUEL_REF)


@dataclass
class MaintenanceReport:
    quarantined: list[str] = field(default_factory=list)
    rebuilt: int = 0
    graded: int = 0
    calibrated: int = 0
    tuned: int = 0

    def summary(self) -> str:
        parts = []
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} bad learned values quarantined")
        if self.rebuilt:
            parts.append(f"{self.rebuilt} learned values rebuilt from stints")
        if self.graded:
            parts.append(f"{self.graded} sessions graded")
        if self.calibrated:
            parts.append(f"{self.calibrated} learned values refit")
        if self.tuned:
            parts.append(f"{self.tuned} rule cooldowns adjusted")
        return ", ".join(parts) if parts else "learned state clean"


def _th(th: Mapping[str, object], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default


def _stint_param(name: str) -> str:
    return name.split("@", 1)[0]


def _bad_reason(p: ModelParam, th: Mapping[str, object]) -> str:
    if (
        p.track_id == TUNE_TRACK
        and p.compound == TUNE_COMPOUND
        and p.name.startswith((COOLDOWN_PREFIX, AB_PREFIX))
    ):
        return ""
    if p.track_id < 0:
        return "unknown_track"
    base = _stint_param(p.name)
    deg_max = _th(th, "deg_max_ms_per_lap", 600)
    if base == "deg_ms_per_lap" and not 0 < p.value < deg_max:
        return "deg_clamped"
    if base == "fuel_ms_per_lap" and not 0 <= p.value < deg_max:
        return "fuel_clamped"
    if base == "base_ms" and not (
        _th(th, "learn_base_min_ms", 40_000) <= p.value <= _th(th, "learn_base_max_ms", 200_000)
    ):
        return "base_implausible"
    return ""


def quarantine_bad(db: Database, th: Mapping[str, object]) -> list[str]:
    out = []
    for p in db.all_params():
        reason = _bad_reason(p, th)
        if reason:
            db.quarantine_param(p, reason)
            out.append(_describe(p, reason))
    return out


def _race_laps(session_type: int, total_laps: int) -> int:
    kind = session_kind(session_type)
    return total_laps if kind == "race" and total_laps > 0 else 0


def _describe(p: ModelParam, reason: str) -> str:
    return f"track {p.track_id} c{p.compound} {p.name}={p.value:g} ({reason})"


def rebuild_stint_params(db: Database, th: Mapping[str, object]) -> tuple[int, list[str]]:
    """Recompute stint-derived priors from `stints`, oldest first. Returns the
    number of rebuilt values and the bad ones found on the way."""
    bad = []
    for p in db.all_params():
        if _stint_param(p.name) in STINT_PARAMS:
            reason = _bad_reason(p, th)
            db.quarantine_param(p, reason or "rebuilt")
            if reason:
                bad.append(_describe(p, reason))
    cap = _th(th, "param_weight_cap", 50)
    names: set[tuple[int, int, str]] = set()
    for row in db.learning_stints():
        track_id = int(row["track_id"] if row["track_id"] is not None else -1)
        if track_id < 0:
            continue
        try:
            params = json.loads(str(row["deg_params"] or "{}"))
        except json.JSONDecodeError:
            continue
        n = int(row["n_valid_laps"] or 0)
        fit = DegFit(
            base_ms=float(params.get("base_ms", 0.0)),
            deg_ms_per_lap=float(params.get("deg_ms_per_lap", 0.0)),
            fuel_ms_per_lap=float(params.get("fuel_ms_per_lap", 0.0)),
            n=n,
            rmse_ms=float(params.get("rmse_ms", 0.0)),
            confidence=float(params.get("confidence", 0.0)),
            source=str(params.get("source", "")),
            fuel_fitted=bool(params.get("fuel_fitted", False)),
        )
        uid = int(row["session_uid"])
        start, end = int(row["start_lap"]), int(row["end_lap"])
        compound = int(row["compound"] or 0)
        laps = [lap for lap in db.laps_for(uid) if start <= lap.lap_num <= end]
        if laps:
            fit = fit_stint(
                laps,
                fit,
                min_laps=int(_th(th, "deg_min_laps", 3)),
                fuel_coeff_fixed=None,
                deg_max_ms_per_lap=_th(th, "deg_max_ms_per_lap", 600),
                deg_rmse_bad_ms=_th(th, "deg_rmse_bad_ms", 800),
            )
            n = fit.n
            db.upsert_stint(
                uid,
                0,
                compound,
                start,
                end,
                fit,
                setup_state_id=majority_state(laps),
            )
        if n <= 0 or not fit_is_clean(
            fit,
            deg_max_ms_per_lap=_th(th, "deg_max_ms_per_lap", 600),
            deg_rmse_bad_ms=_th(th, "deg_rmse_bad_ms", 800),
            base_min_ms=_th(th, "learn_base_min_ms", 40_000),
            base_max_ms=_th(th, "learn_base_max_ms", 200_000),
        ):
            continue
        race_laps = _race_laps(int(row["session_type"] or 0), int(row["total_laps"] or 0))
        for name, value in zip(
            STINT_PARAMS,
            (fit.deg_ms_per_lap, fit.base_ms, fit.fuel_ms_per_lap, fit.fuel_ms_per_lap),
            strict=True,
        ):
            if name == "fuel_ms_per_lap" and not fit.fuel_fitted:
                continue
            key = scoped(name, race_laps)
            db.fold_param(track_id, compound, key, value, weight=float(n), param_weight_cap=cap)
            names.add((track_id, compound, key))
    return len(names), bad


def grade_ungraded(db: Database, th: Mapping[str, object]) -> int:
    uids = db.ungraded_sessions()
    for uid in uids:
        grade_and_store(db, uid, th)
        db.mark_graded(uid)
    return len(uids)


def calibrate_learned(db: Database, settings: Settings) -> int:
    """Refit track priors from stored laps. Returns the number of values that
    changed. Values that would be quarantined are never written."""
    th = settings.thresholds

    def accept(track_id: int, compound: int, name: str, value: float) -> bool:
        return not _bad_reason(ModelParam(track_id, compound, name, value, 0.0, 0.0), th)

    before = {(p.track_id, p.compound, p.name): (p.value, p.weight) for p in db.all_params()}
    report = calibrate(db, settings, accept=accept)
    return sum(
        before.get((track["track_id"], w["compound"], w["name"])) != (w["value"], w["weight"])
        for track in report["tracks"]
        for w in track["writes"]
    )


def tune_learned(db: Database, th: Mapping[str, object]) -> int:
    """Refold rule cooldowns. Returns the number of rules whose multiplier changed."""
    before = load_cooldown_mults(db)
    tune_from_db(db, th)
    after = load_cooldown_mults(db)
    return sum(before.get(rule) != mult for rule, mult in after.items())


def mid_session(db: Database, settings: Settings, wall_now: float | None = None) -> bool:
    """True when a fresh heartbeat shows a session is still running (watchdog restart)."""
    hb = db.read_heartbeat()
    if hb is None:
        return False
    wall_now = time.time() if wall_now is None else wall_now
    return wall_now - hb.wall_t <= settings.engine.recovery_max_age_s


def maintain(db: Database, settings: Settings, *, refit: bool = True) -> MaintenanceReport:
    th = settings.thresholds
    report = MaintenanceReport()
    with db.transaction():
        if db.maintenance_version("learn_rebuild") < LEARN_VERSION:
            report.rebuilt, report.quarantined = rebuild_stint_params(db, th)
            db.set_maintenance_version("learn_rebuild", LEARN_VERSION)
        report.quarantined += quarantine_bad(db, th)
        report.graded = grade_ungraded(db, th)
        if refit and settings.learning.auto_calibrate:
            report.calibrated = calibrate_learned(db, settings)
            report.tuned = tune_learned(db, th)
    return report
