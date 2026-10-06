"""Automatic learned-state upkeep, run on every `pitwall start` and after each
session: nobody should need `pitwall digest` or SQLite to keep priors sane.

- quarantine: model_params rows that fail a sanity check (unknown track,
  slope at the fit clamp, implausible lap time) move to
  model_params_quarantine and stop feeding priors;
- rebuild (once per LEARN_VERSION): each stored stint is refit from its laps
  with today's model, then stint-derived priors are recomputed with today's
  gate and race-distance scoping;
- grade: sessions without hindsight outcomes are graded.

Deterministic and idempotent: a second run changes nothing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from pitwall.hindsight import grade_and_store
from pitwall.model.deg import DEG_FUEL_REF, DegFit, fit_is_clean, fit_stint, scoped
from pitwall.protocol.enums import SessionType
from pitwall.store.db import Database, ModelParam
from pitwall.tune import AB_PREFIX, COOLDOWN_PREFIX, TUNE_COMPOUND, TUNE_TRACK

LEARN_VERSION = 2
STINT_PARAMS = ("deg_ms_per_lap", "base_ms", "fuel_ms_per_lap", DEG_FUEL_REF)


@dataclass
class MaintenanceReport:
    quarantined: list[str] = field(default_factory=list)
    rebuilt: int = 0
    graded: int = 0

    def summary(self) -> str:
        parts = []
        if self.quarantined:
            parts.append(f"{len(self.quarantined)} bad learned values quarantined")
        if self.rebuilt:
            parts.append(f"{self.rebuilt} learned values rebuilt from stints")
        if self.graded:
            parts.append(f"{self.graded} sessions graded")
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
    try:
        kind = SessionType(session_type).kind()
    except ValueError:
        return 0
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
            db.upsert_stint(uid, 0, compound, start, end, fit)
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


def maintain(db: Database, th: Mapping[str, object]) -> MaintenanceReport:
    report = MaintenanceReport()
    with db.transaction():
        if db.maintenance_version("learn_rebuild") < LEARN_VERSION:
            report.rebuilt, report.quarantined = rebuild_stint_params(db, th)
            db.set_maintenance_version("learn_rebuild", LEARN_VERSION)
        report.quarantined += quarantine_bad(db, th)
        report.graded = grade_ungraded(db, th)
    return report
