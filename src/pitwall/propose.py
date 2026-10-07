"""Human-reviewed threshold candidates from calibrated corpus and replay diffs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pitwall.calibrate import calibrate
from pitwall.config.models import Settings
from pitwall.diff import run_diff
from pitwall.questions import question_candidates
from pitwall.store.db import Database


def _number(settings: Settings, key: str, default: float) -> float:
    value = settings.thresholds.get(key, default)
    return float(value) if isinstance(value, int | float) else default


def propose_thresholds(
    db: Database, settings: Settings, candidate_rules: Path | None = None
) -> dict[str, Any]:
    fits = calibrate(db, settings, dry_run=True)
    minimum = int(_number(settings, "prior_min_weight", 2))
    tracks: list[dict[str, Any]] = []
    for track in fits["tracks"]:
        th: dict[str, Any] = {}
        cold: dict[int, float] = {}
        hot: dict[int, float] = {}
        for compound, values in track["compounds"].items():
            thermal = values.get("thermal")
            if not track["converged"] or thermal is None or int(thermal["n_laps"]) < minimum:
                continue
            cold[int(compound)] = round(float(thermal["thermal_lo_c"]), 2)
            hot[int(compound)] = round(float(thermal["thermal_hi_c"]), 2)
        if cold:
            th["tyre_inner_cold_by_compound_c"] = cold
            th["tyre_inner_hot_by_compound_c"] = hot
        energy = track.get("energy")
        if energy is not None and int(energy["n_laps"]) >= minimum:
            width = (
                float(energy["energy_deployed_j_p75"]) - float(energy["energy_deployed_j_p25"])
            ) / 2
            th["energy_over_tolerance_j"] = (
                round(
                    max(
                        _number(settings, "energy_tol_min_j", 100_000),
                        min(_number(settings, "energy_tol_max_j", 600_000), width),
                    )
                    / 10_000
                )
                * 10_000
            )
        if th:
            tracks.append(
                {
                    "track_id": track["track_id"],
                    "sessions": track["sessions"],
                    "laps": track["n_laps"],
                    "converged": track["converged"],
                    "thresholds": th,
                }
            )
    result: dict[str, Any] = {
        "review_required": True,
        "applied": False,
        "track_overlays": tracks,
        "question_candidates": question_candidates(
            db,
            settings.menu,
            settings.rules,
            thresholds=settings.thresholds,
        ),
        "note": (
            "Proposals are pace-derived, not measured MFD colour boundaries. "
            "Review alongside in-game colours and replay before editing YAML."
        ),
    }
    if candidate_rules is not None:
        recordings = sorted(
            {
                Path(str(session["recording_path"]))
                for session in db.sessions()
                if session.get("recording_path") and Path(str(session["recording_path"])).is_file()
            }
        )
        result["diff"] = run_diff(recordings, None, candidate_rules)
        result["diff_recordings"] = len(recordings)
    return result
