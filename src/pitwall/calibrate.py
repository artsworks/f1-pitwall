"""Deterministic track calibration from persisted session laps."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from pitwall.config.models import Settings
from pitwall.config.thresholds import threshold as _th
from pitwall.derive import is_synthetic_uid
from pitwall.hindsight import linear_deg, stints, stop_laps
from pitwall.model.deg import DEG_FUEL_REF, fuel_burned_laps, scoped
from pitwall.protocol.enums import session_kind
from pitwall.store.db import Database, LapRow

WriteFilter = Callable[[int, int, str, float], bool]


def _green(lap: LapRow) -> bool:
    return lap.valid == 1 and lap.sc_status == 0 and lap.lap_time_ms > 0


def _fuel_burn(
    sessions: Sequence[tuple[dict[str, Any], list[LapRow]]],
    *,
    min_delta_kg: float,
    max_delta_kg: float,
) -> tuple[float, int]:
    deltas: list[float] = []
    for _, laps in sessions:
        green = [lap for lap in laps if _green(lap)]
        for before, after in zip(green, green[1:], strict=False):
            delta = before.fuel_kg - after.fuel_kg
            if after.lap_num == before.lap_num + 1 and min_delta_kg < delta < max_delta_kg:
                deltas.append(delta)
    return (statistics.median(deltas), len(deltas)) if deltas else (0.0, 0)


def _fit_pooled(
    sessions: Sequence[tuple[dict[str, Any], list[LapRow]]],
    fuel_ms_per_kg_max: float,
) -> dict[str, Any]:
    by_group: dict[tuple[int, int], list[LapRow]] = defaultdict(list)
    for session, laps in sessions:
        distance = _race_laps(session)
        for lap in laps:
            if _green(lap):
                by_group[(lap.compound, distance)].append(lap)
    groups = sorted(by_group)
    if not groups:
        return {"compounds": {}, "groups": [], "k_ms_per_kg": None, "k_identifiable": False, "n": 0}
    indices = {group: index for index, group in enumerate(groups)}
    column_count = 2 * len(groups) + 1
    matrix: list[list[float]] = []
    target: list[float] = []
    for group in groups:
        for lap in by_group[group]:
            row = [0.0] * column_count
            idx = indices[group]
            row[2 * idx] = 1.0
            row[2 * idx + 1] = float(lap.tyre_age_laps)
            row[-1] = float(lap.fuel_kg)
            matrix.append(row)
            target.append(float(lap.lap_time_ms))
    design = np.asarray(matrix, dtype=np.float64)
    times = np.asarray(target, dtype=np.float64)
    coefficients, _, rank, _ = np.linalg.lstsq(design, times, rcond=None)
    identifiable = int(rank) == column_count
    raw_k = float(coefficients[-1])
    k_value = min(max(raw_k, 0.0), fuel_ms_per_kg_max) if identifiable else None
    if k_value is not None and k_value != raw_k:
        coefficients = np.linalg.lstsq(design[:, :-1], times - k_value * design[:, -1], rcond=None)[
            0
        ]
    result: dict[int, dict[str, float | int]] = {}
    fitted_groups = []
    for compound, distance in groups:
        idx = indices[(compound, distance)]
        values = {
            "compound": compound,
            "race_laps": distance,
            "base_ms": float(coefficients[2 * idx]),
            "deg_ms_per_lap": float(coefficients[2 * idx + 1]),
            "n_laps": len(by_group[(compound, distance)]),
        }
        fitted_groups.append(values)
        if compound not in result or distance == 0:
            result[compound] = values
    return {
        "compounds": result,
        "groups": fitted_groups,
        "k_ms_per_kg": k_value,
        "k_raw_ms_per_kg": raw_k,
        "k_identifiable": identifiable,
        "n": len(target),
    }


def _thermal_window(
    sessions: Sequence[tuple[dict[str, Any], list[LapRow]]],
    compound: int,
    fuel_ms_per_lap: float,
    bin_width: float,
    min_stint_laps: int,
    min_laps_per_bin: int,
    tolerance_ms: float,
) -> dict[str, Any] | None:
    bins: dict[int, list[float]] = defaultdict(list)
    for _, laps in sessions:
        grouped = stints(laps, stop_laps(laps))
        for stint in grouped:
            green = [lap for lap in stint if _green(lap)]
            if len(green) < min_stint_laps:
                continue
            if any(lap.compound != compound for lap in green):
                continue
            fit = linear_deg(stint, fuel_ms_per_lap)
            if fit is None:
                continue
            base_ms, deg_ms, _ = fit
            for lap, burned in zip(green, fuel_burned_laps(green), strict=True):
                if lap.tyre_inner_c == 0:
                    continue
                corrected = lap.lap_time_ms + fuel_ms_per_lap * max(0.0, burned)
                residual = corrected - (base_ms + deg_ms * lap.tyre_age_laps)
                bins[math.floor(lap.tyre_inner_c / bin_width)].append(residual)
    means = {
        index: statistics.fmean(values)
        for index, values in bins.items()
        if len(values) >= min_laps_per_bin
    }
    if not means:
        return None
    best = min(means, key=lambda index: (means[index], index))
    threshold = means[best] + tolerance_ms
    low = high = best
    while low - 1 in means and means[low - 1] <= threshold:
        low -= 1
    while high + 1 in means and means[high + 1] <= threshold:
        high += 1
    n = sum(len(bins[index]) for index in range(low, high + 1))
    return {
        "thermal_lo_c": low * bin_width,
        "thermal_hi_c": (high + 1) * bin_width,
        "n_laps": n,
        "bin_means": {str(index): means[index] for index in sorted(means)},
    }


def _energy_map(
    sessions: Sequence[tuple[dict[str, Any], list[LapRow]]],
) -> dict[str, Any] | None:
    values = [
        float(lap.ers_deployed_j)
        for session, laps in sessions
        if _is_race(int(session.get("session_type") or 0))
        for lap in laps
        if _green(lap) and lap.ers_deployed_j > 0
    ]
    if not values:
        return None
    p25, p50, p75 = np.percentile(np.asarray(values, dtype=np.float64), [25, 50, 75])
    return {
        "energy_deployed_j_p25": float(p25),
        "energy_deployed_j_p50": float(p50),
        "energy_deployed_j_p75": float(p75),
        "n_laps": len(values),
    }


def _is_race(session_type: int) -> bool:
    return session_kind(session_type) == "race"


def _race_laps(session: Mapping[str, object]) -> int:
    session_type = session.get("session_type")
    total_laps = session.get("total_laps")
    if isinstance(session_type, int) and _is_race(session_type):
        return total_laps if isinstance(total_laps, int) and total_laps > 0 else -1
    return 0


def _fit_sessions(
    sessions: Sequence[tuple[dict[str, Any], list[LapRow]]],
    settings: Settings,
) -> dict[str, Any]:
    thresholds = settings.thresholds
    fit = _fit_pooled(
        sessions,
        _th(thresholds, "calib_fuel_ms_per_kg_max", 80.0),
    )
    fuel_burn, fuel_n = _fuel_burn(
        sessions,
        min_delta_kg=_th(thresholds, "fuel_delta_min_kg", 0),
        max_delta_kg=_th(thresholds, "fuel_delta_max_kg", 10),
    )
    min_bin_laps = int(_th(thresholds, "calib_thermal_min_laps_per_bin", 3))
    thermal: dict[int, dict[str, Any]] = {}
    for comp_fit in fit["groups"]:
        compound = int(comp_fit["compound"])
        group_sessions = [
            entry for entry in sessions if _race_laps(entry[0]) == comp_fit["race_laps"]
        ]
        group_burn, group_n = _fuel_burn(
            group_sessions,
            min_delta_kg=_th(thresholds, "fuel_delta_min_kg", 0),
            max_delta_kg=_th(thresholds, "fuel_delta_max_kg", 10),
        )
        comp_fit["fuel_kg_per_lap"] = group_burn
        comp_fit["fuel_burn_n"] = group_n
        thermal_window = _thermal_window(
            sessions,
            compound,
            float(fit["k_ms_per_kg"] or 0.0) * fuel_burn,
            _th(thresholds, "calib_thermal_bin_c", 5.0),
            int(_th(thresholds, "calib_thermal_min_stint_laps", 3)),
            min_bin_laps,
            _th(thresholds, "calib_thermal_tolerance_ms", 150.0),
        )
        if thermal_window is not None:
            thermal[compound] = thermal_window
        comp_fit["base_ms"] = float(comp_fit["base_ms"])
        comp_fit["deg_ms_per_lap"] = min(
            max(float(comp_fit["deg_ms_per_lap"]), 0.0),
            _th(thresholds, "deg_max_ms_per_lap", 600.0),
        )
    return {
        **fit,
        "fuel_kg_per_lap": fuel_burn,
        "fuel_burn_n": fuel_n,
        "thermal": thermal,
        "energy": _energy_map(sessions),
    }


def _relative_delta(before: float, after: float) -> float:
    return abs(after - before) / max(abs(before), 1e-9)


def _convergence(
    ordered_sessions: Sequence[tuple[dict[str, Any], list[LapRow]]],
    settings: Settings,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    value_histories: dict[str, list[float]] = defaultdict(list)
    for count in range(1, len(ordered_sessions) + 1):
        fit = _fit_sessions(ordered_sessions[:count], settings)
        values: dict[str, float] = {}
        for params in fit["groups"]:
            if int(params["race_laps"]) < 0:
                continue
            compound = int(params["compound"])
            prefix = scoped(f"c{compound}", int(params["race_laps"]))
            values[f"{prefix}.base_ms"] = float(params["base_ms"])
            values[f"{prefix}.deg_ms_per_lap"] = float(params["deg_ms_per_lap"])
            thermal = fit["thermal"].get(compound)
            if thermal is not None:
                values[f"c{compound}.thermal_lo_c"] = float(thermal["thermal_lo_c"])
                values[f"c{compound}.thermal_hi_c"] = float(thermal["thermal_hi_c"])
        if fit["k_ms_per_kg"] is not None:
            values["fuel_ms_per_kg"] = float(fit["k_ms_per_kg"])
            if fit["fuel_burn_n"]:
                fuel_ms = float(fit["k_ms_per_kg"]) * float(fit["fuel_kg_per_lap"])
                for compound in sorted(fit["compounds"]):
                    values[f"c{compound}.fuel_ms_per_lap"] = fuel_ms
        if fit["fuel_burn_n"]:
            values["fuel_kg_per_lap"] = float(fit["fuel_kg_per_lap"])
        energy = fit["energy"]
        if energy is not None:
            for name in (
                "energy_deployed_j_p25",
                "energy_deployed_j_p50",
                "energy_deployed_j_p75",
            ):
                values[name] = float(energy[name])
        for name, value in values.items():
            value_histories[name].append(value)
        history.append({"sessions": count, "values": values})
    window = int(_th(settings.thresholds, "calib_converge_window", 3))
    tolerance = _th(settings.thresholds, "calib_converge_tol_frac", 0.1)
    stable: dict[str, list[float]] = {}
    for name, fitted_values in value_histories.items():
        deltas = [
            _relative_delta(a, b) for a, b in zip(fitted_values, fitted_values[1:], strict=False)
        ]
        stable[name] = deltas[-window:]
    converged = bool(stable) and all(
        len(deltas) == window and all(delta <= tolerance for delta in deltas)
        for deltas in stable.values()
    )
    return {"converged": converged, "history": history, "relative_deltas": stable}


def calibrate_track(
    db: Database,
    track_id: int,
    settings: Settings,
    *,
    dry_run: bool = False,
    accept: WriteFilter | None = None,
    include_synthetic: bool = False,
) -> dict[str, Any]:
    """Fit one track and optionally write only sufficiently supported values.
    `accept(track_id, compound, name, value)` can veto individual writes."""
    rows = db.sessions_for_track(track_id)
    synthetic_rows = [
        row for row in rows if bool(row.get("synthetic")) or is_synthetic_uid(int(row["uid"]))
    ]
    if not include_synthetic:
        rows = [row for row in rows if row not in synthetic_rows]
    max_sessions = int(_th(settings.thresholds, "calib_max_sessions", 20))
    ordered = sorted(rows, key=lambda row: (float(row.get("started_at") or 0.0), int(row["uid"])))
    recent = ordered[-max_sessions:]
    sessions = [(row, db.laps_for(int(row["uid"]))) for row in recent]
    fit = _fit_sessions(sessions, settings)
    convergence = _convergence(sessions, settings)
    need = max(
        int(_th(settings.thresholds, "prior_min_weight", 2)),
        int(_th(settings.thresholds, "deg_min_laps", 3)),
    )
    energy_need = int(_th(settings.thresholds, "prior_min_weight", 2))
    output: dict[str, Any] = {
        "track_id": track_id,
        "sessions": len(sessions),
        "synthetic_skipped": 0 if include_synthetic else len(synthetic_rows),
        "n_laps": fit["n"],
        "need": need,
        "k_identifiable": fit["k_identifiable"],
        "k_ms_per_kg": fit["k_ms_per_kg"],
        "fuel_kg_per_lap": fit["fuel_kg_per_lap"],
        "fuel_burn_n": fit["fuel_burn_n"],
        "compounds": {},
        "race_distances": {},
        "energy": fit["energy"],
        **convergence,
    }
    writes: list[tuple[int, str, float, float]] = []
    for params in fit["groups"]:
        compound = int(params["compound"])
        distance = int(params["race_laps"])
        n = int(params["n_laps"])
        gate = n >= need and fit["k_identifiable"] and distance >= 0
        values = {
            "base_ms": float(params["base_ms"]),
            "deg_ms_per_lap": float(params["deg_ms_per_lap"]),
        }
        status = (
            "ready"
            if gate
            else (
                f"insufficient ({n}/{need})"
                if n < need
                else (
                    "race length unknown" if distance < 0 else "fuel and tyre wear not identifiable"
                )
            )
        )
        if (
            fit["k_identifiable"]
            and gate
            and fit["k_ms_per_kg"] is not None
            and params["fuel_burn_n"] >= need
        ):
            values["fuel_ms_per_lap"] = float(fit["k_ms_per_kg"]) * float(params["fuel_kg_per_lap"])
        thermal = fit["thermal"].get(compound)
        if thermal is not None and int(thermal["n_laps"]) >= energy_need:
            values.update(
                thermal_lo_c=float(thermal["thermal_lo_c"]),
                thermal_hi_c=float(thermal["thermal_hi_c"]),
            )
        if not dry_run:
            for name, value in values.items():
                value_gate = (
                    gate
                    if name in ("base_ms", "deg_ms_per_lap", "fuel_ms_per_lap")
                    else (thermal is not None and int(thermal["n_laps"]) >= energy_need)
                )
                if value_gate:
                    weight_n = (
                        int(thermal["n_laps"])
                        if name in ("thermal_lo_c", "thermal_hi_c") and thermal is not None
                        else n
                    )
                    writes.append(
                        (
                            compound,
                            scoped(name, distance)
                            if name in ("base_ms", "deg_ms_per_lap", "fuel_ms_per_lap")
                            else name,
                            value,
                            float(
                                min(
                                    weight_n,
                                    int(_th(settings.thresholds, "param_weight_cap", 50)),
                                )
                            ),
                        )
                    )
        group_output = {
            **values,
            "n_laps": n,
            "status": status,
            "thermal": thermal,
            "race_laps": distance,
        }
        if distance:
            output["race_distances"].setdefault(distance, {})[compound] = group_output
        if compound not in output["compounds"] or distance == 0:
            output["compounds"][compound] = group_output
    if fit["fuel_burn_n"] >= need and not dry_run:
        writes.append(
            (
                0,
                "fuel_kg_per_lap",
                float(fit["fuel_kg_per_lap"]),
                float(
                    min(
                        fit["fuel_burn_n"],
                        int(_th(settings.thresholds, "param_weight_cap", 50)),
                    )
                ),
            )
        )
    energy = fit["energy"]
    if energy is not None and int(energy["n_laps"]) >= energy_need and not dry_run:
        for name in (
            "energy_deployed_j_p25",
            "energy_deployed_j_p50",
            "energy_deployed_j_p75",
        ):
            writes.append(
                (
                    0,
                    name,
                    float(energy[name]),
                    float(
                        min(
                            int(energy["n_laps"]),
                            int(_th(settings.thresholds, "param_weight_cap", 50)),
                        )
                    ),
                )
            )
    fuel_by_compound = {
        (c, name.removeprefix("fuel_ms_per_lap")): v
        for c, name, v, _ in writes
        if name.split("@", 1)[0] == "fuel_ms_per_lap"
    }
    for c, name, _, w in list(writes):
        suffix = name.removeprefix("deg_ms_per_lap")
        if name.split("@", 1)[0] == "deg_ms_per_lap" and (c, suffix) in fuel_by_compound:
            writes.append((c, DEG_FUEL_REF + suffix, fuel_by_compound[(c, suffix)], w))
    if accept is not None:
        writes = [w for w in writes if accept(track_id, w[0], w[1], w[2])]
    if not dry_run:
        for compound, name, value, weight in writes:
            db.set_param(track_id, compound, name, value, weight)
    output["writes"] = [
        {"compound": compound, "name": name, "value": value, "weight": weight}
        for compound, name, value, weight in writes
    ]
    output["dry_run"] = dry_run
    return output


def calibrate(
    db: Database,
    settings: Settings,
    *,
    track_id: int | None = None,
    dry_run: bool = False,
    accept: WriteFilter | None = None,
    include_synthetic: bool = False,
) -> dict[str, Any]:
    allowed_sessions = [
        row
        for row in db.sessions()
        if include_synthetic
        or not bool(row.get("synthetic"))
        and not is_synthetic_uid(int(row["uid"]))
    ]
    tracks = (
        [track_id]
        if track_id is not None
        else sorted(
            {int(row["track_id"]) for row in allowed_sessions if row.get("track_id") is not None}
        )
    )
    return {
        "tracks": [
            calibrate_track(
                db,
                track,
                settings,
                dry_run=dry_run,
                accept=accept,
                include_synthetic=include_synthetic,
            )
            for track in tracks
        ],
        "dry_run": dry_run,
    }


def write_overlays(
    report: Mapping[str, Any],
    settings: Settings,
    overlay_dir: Path | None = None,
) -> list[Path]:
    def sorted_yaml(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: sorted_yaml(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            return [sorted_yaml(item) for item in value]
        return value

    root = overlay_dir or (Path.home() / ".pitwall" / "tracks")
    thresholds = settings.thresholds
    energy_min = _th(thresholds, "energy_tol_min_j", 100_000)
    energy_max = _th(thresholds, "energy_tol_max_j", 600_000)
    paths: list[Path] = []
    for track in report.get("tracks", []):
        track_id = int(track["track_id"])
        path = root / f"{track_id}.yaml"
        existing = yaml.safe_load(path.read_text()) if path.exists() else {}
        data = existing if isinstance(existing, dict) else {}
        changed = False
        fuel_n = int(track.get("fuel_burn_n", 0))
        if fuel_n >= int(track.get("need", 1)):
            data["fuel_kg_per_lap"] = round(float(track["fuel_kg_per_lap"]), 4)
            changed = True
        raw_degs = data.get("deg_ms_per_lap", {})
        compound_degs = (
            {
                int(key) if isinstance(key, str) and key.isdecimal() else key: value
                for key, value in raw_degs.items()
            }
            if isinstance(raw_degs, dict)
            else {}
        )
        thermal_cold: dict[int, float] = {}
        thermal_hot: dict[int, float] = {}
        thermal_need = int(_th(thresholds, "prior_min_weight", 2))
        for key, values in track.get("compounds", {}).items():
            compound = int(key)
            n = int(values["n_laps"])
            if (
                n >= int(track.get("need", 1))
                and values.get("status") == "ready"
                and not values.get("race_laps")
            ):
                compound_degs[compound] = round(float(values["deg_ms_per_lap"]), 1)
                changed = True
            thermal = values.get("thermal")
            if thermal is not None and int(thermal["n_laps"]) >= thermal_need:
                thermal_cold[compound] = round(float(thermal["thermal_lo_c"]), 2)
                thermal_hot[compound] = round(float(thermal["thermal_hi_c"]), 2)
        if compound_degs:
            data["deg_ms_per_lap"] = dict(
                sorted(compound_degs.items(), key=lambda item: str(item[0]))
            )
        elif "deg_ms_per_lap" in data:
            data["deg_ms_per_lap"] = compound_degs
        if thermal_cold:
            th = dict(data.get("thresholds", {}))
            th["tyre_inner_cold_by_compound_c"] = {
                **th.get("tyre_inner_cold_by_compound_c", {}),
                **thermal_cold,
            }
            th["tyre_inner_hot_by_compound_c"] = {
                **th.get("tyre_inner_hot_by_compound_c", {}),
                **thermal_hot,
            }
            data["thresholds"] = th
            changed = True
        energy = track.get("energy")
        if energy is not None and int(energy["n_laps"]) >= int(
            _th(thresholds, "prior_min_weight", 2)
        ):
            energy_tol = (
                round(
                    (
                        float(energy["energy_deployed_j_p75"])
                        - float(energy["energy_deployed_j_p25"])
                    )
                    / 2
                    / 10_000
                )
                * 10_000
            )
            th = dict(data.get("thresholds", {}))
            th["energy_over_tolerance_j"] = int(min(energy_max, max(energy_min, energy_tol)))
            data["thresholds"] = th
            changed = True
        if not changed:
            continue
        root.mkdir(parents=True, exist_ok=True)
        ordered = sorted_yaml(data)
        path.write_text(yaml.safe_dump(ordered, sort_keys=False))
        paths.append(path)
    return paths


def format_calibration(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for track in report.get("tracks", []):
        lines.append(
            f"Track {track['track_id']}: sessions={track['sessions']} laps={track['n_laps']} "
            f"converged={str(track['converged']).lower()}"
        )
        lines.append("prefix | fitted values")
        for entry in track["history"]:
            values = ", ".join(
                f"{name}={value:.1f}" for name, value in sorted(entry["values"].items())
            )
            lines.append(f"{entry['sessions']:>6} | {values}")
        if not track["k_identifiable"]:
            lines.append("fuel coefficient: not identifiable")
        for compound, values in sorted(track["compounds"].items()):
            lines.append(
                f"  compound {compound}: {values['status']}; "
                f"deg={values['deg_ms_per_lap']:.1f} ms/lap"
            )
    return "\n".join(lines)
