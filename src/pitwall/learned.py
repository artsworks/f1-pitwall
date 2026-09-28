"""Read-only summary of persisted model and feedback state."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from pitwall.config.models import Settings
from pitwall.model.deg import Prior, resolve_prior
from pitwall.state.session import thermal_window
from pitwall.store.db import Database
from pitwall.strategy.battle import HOLD, PASS_COMPOUND, PASS_DRS, PASS_NODRS
from pitwall.tune import COOLDOWN_PREFIX, TUNE_COMPOUND, TUNE_TRACK


def _th(thresholds: Mapping[str, object], name: str, default: float) -> float:
    value = thresholds.get(name, default)
    return float(value) if isinstance(value, int | float) else default


def _yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def _track_overlays(track_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults_dir = Path(__file__).parent / "config" / "defaults" / "tracks"
    defaults = _yaml(defaults_dir / f"{track_id}.yaml")
    user = _yaml(Path.home() / ".pitwall" / "tracks" / f"{track_id}.yaml")
    effective = {**defaults, **user}
    effective["pit_loss_s"] = {
        **dict(defaults.get("pit_loss_s", {})),
        **dict(user.get("pit_loss_s", {})),
    }
    effective["deg_ms_per_lap"] = {
        **dict(defaults.get("deg_ms_per_lap", {})),
        **dict(user.get("deg_ms_per_lap", {})),
    }
    effective["thresholds"] = {
        **dict(defaults.get("thresholds", {})),
        **dict(user.get("thresholds", {})),
    }
    return defaults, effective


def _param(
    db: Database,
    track_id: int,
    compound: int,
    name: str,
    default: float,
    overlay: float | None = None,
    min_weight: float = 2.0,
) -> dict[str, Any]:
    prior: Prior = resolve_prior(
        db,
        track_id,
        compound,
        name,
        overlay_value=overlay,
        default=default,
        min_weight=min_weight,
    )
    raw = db.get_param(track_id, compound, name)
    return {
        "value": prior.value,
        "weight": raw.weight if raw is not None else prior.weight,
        "source": prior.source,
        "default": default,
        "overlay": overlay,
        "delta_to_default": prior.value - default,
    }


def learned_state(db: Database, settings: Settings, track_id: int | None = None) -> dict[str, Any]:
    """Return JSON-safe learned values, sources, and evidence counts."""
    sessions = db.sessions(track_id)
    params = db.all_params()
    tracks = {
        int(row["track_id"])
        for row in sessions
        if row.get("track_id") is not None and int(row["track_id"]) >= 0
    }
    tracks.update(param.track_id for param in params if param.track_id >= 0)
    if track_id is not None:
        tracks &= {track_id}
    grades = Counter(str(row["rule_id"]) for row in db.all_grades())
    tracks_out: list[dict[str, Any]] = []
    min_weight = _th(settings.thresholds, "prior_min_weight", 2.0)
    for current_track in sorted(tracks):
        track_sessions = db.sessions_for_track(current_track)
        packaged, overlay = _track_overlays(current_track)
        name = str(packaged.get("name") or overlay.get("name") or f"track-{current_track}")
        compounds = {
            param.compound
            for param in params
            if param.track_id == current_track and param.compound >= 0
        }
        for session in track_sessions:
            for lap in db.laps_for(int(session["uid"])):
                compounds.add(lap.compound)
        compound_values: dict[int, Any] = {}
        default_deg = _th(settings.thresholds, "deg_default_ms_per_lap", 80.0)
        default_base = _th(settings.thresholds, "release_fallback_lap_s", 95.0) * 1000.0
        default_fuel_ms = _th(settings.thresholds, "fuel_ms_per_lap_default", 30.0)
        overlay_degs = overlay.get("deg_ms_per_lap", {})
        for compound in sorted(compounds):
            deg_overlay = overlay_degs.get(compound, overlay_degs.get(str(compound)))
            values: dict[str, Any] = {}
            for key, default, overlay_value in (
                (
                    "deg_ms_per_lap",
                    default_deg,
                    float(deg_overlay) if deg_overlay is not None else None,
                ),
                (
                    "base_ms",
                    default_base,
                    (
                        float(overlay["base_pace_ms"])
                        if float(overlay.get("base_pace_ms", 0) or 0) > 0
                        else None
                    ),
                ),
                ("fuel_ms_per_lap", default_fuel_ms, None),
            ):
                values[key] = _param(
                    db,
                    current_track,
                    compound,
                    key,
                    default,
                    overlay_value,
                    min_weight,
                )
            compound_values[compound] = values
        fuel_overlay_raw = overlay.get("fuel_kg_per_lap")
        fuel_overlay = float(fuel_overlay_raw) if fuel_overlay_raw is not None else None
        fuel = _param(
            db,
            current_track,
            0,
            "fuel_kg_per_lap",
            _th(settings.thresholds, "fuel_kg_per_lap_default", 1.7),
            fuel_overlay,
            min_weight,
        )
        pit_defaults = {
            "green": _th(settings.thresholds, "pit_loss_default_s", 22.0) * 1000.0,
            "sc": _th(settings.thresholds, "pit_loss_sc_default_s", 8.0) * 1000.0,
            "vsc": _th(settings.thresholds, "pit_loss_vsc_default_s", 12.0) * 1000.0,
        }
        pit = {
            mode: _param(
                db,
                current_track,
                0,
                f"pit_loss_{mode}_ms",
                default,
                float(overlay.get("pit_loss_s", {}).get(mode)) * 1000.0
                if mode in overlay.get("pit_loss_s", {})
                else None,
                min_weight,
            )
            for mode, default in pit_defaults.items()
        }
        thermal_defaults = {
            "cold": _th(settings.thresholds, "tyre_inner_cold_c", 80.0),
            "hot": _th(settings.thresholds, "tyre_inner_hot_c", 110.0),
        }
        thermal_overlay = overlay.get("thresholds", {})
        thermal = {
            "yaml_cold_c": float(
                thermal_overlay.get("tyre_inner_cold_c", thermal_defaults["cold"])
            ),
            "yaml_hot_c": float(thermal_overlay.get("tyre_inner_hot_c", thermal_defaults["hot"])),
            "yaml_by_compound_c": {
                compound: thermal_window({**settings.thresholds, **thermal_overlay}, compound)
                for compound in sorted(compounds)
            },
            "compounds": {
                compound: {
                    "lo": _param(db, current_track, compound, "thermal_lo_c", 0.0),
                    "hi": _param(db, current_track, compound, "thermal_hi_c", 0.0),
                }
                for compound in sorted(compounds)
                if db.get_param(current_track, compound, "thermal_lo_c") is not None
                or db.get_param(current_track, compound, "thermal_hi_c") is not None
            },
        }
        energy = {
            name: _param(db, current_track, 0, name, 0.0)
            for name in (
                "energy_deployed_j_p25",
                "energy_deployed_j_p50",
                "energy_deployed_j_p75",
            )
            if db.get_param(current_track, 0, name) is not None
        }
        battle_defaults = {
            PASS_DRS: _th(settings.thresholds, "battle_pass_drs_prior", 0.35),
            PASS_NODRS: _th(settings.thresholds, "battle_pass_nodrs_prior", 0.15),
            HOLD: _th(settings.thresholds, "battle_hold_prior", 0.7),
        }
        battle = {
            name: _param(
                db,
                current_track,
                PASS_COMPOUND,
                name,
                default,
                min_weight=min_weight,
            )
            for name, default in battle_defaults.items()
        }
        tracks_out.append(
            {
                "track_id": current_track,
                "name": name,
                "session_count": len(track_sessions),
                "ingested_count": db.ingested_count(current_track),
                "compounds": compound_values,
                "fuel_kg_per_lap": fuel,
                "pit_loss_ms": pit,
                "thermal": thermal,
                "energy": energy,
                "battle_priors": battle,
                "driver_input_ack_neg_by_rule": db.driver_input_counts(current_track),
            }
        )
    tuned_params = [
        param
        for param in params
        if param.track_id == TUNE_TRACK
        and param.compound == TUNE_COMPOUND
        and param.name.startswith(COOLDOWN_PREFIX)
    ]
    tuned = [
        {
            "rule_id": param.name[len(COOLDOWN_PREFIX) :],
            "multiplier": param.value,
            "weight": param.weight,
            "grade_count": grades.get(param.name[len(COOLDOWN_PREFIX) :], 0),
        }
        for param in tuned_params
    ]
    return {"tracks": tracks_out, "tuned_cooldowns": tuned}


def format_learned(state: Mapping[str, Any]) -> str:
    def prior_text(value: Mapping[str, Any]) -> str:
        overlay = value["overlay"]
        overlay_text = "none" if overlay is None else f"{float(overlay):.2f}"
        return (
            f"{float(value['value']):.2f} ({value['source']}, w={float(value['weight']):.1f}; "
            f"default={float(value['default']):.2f}, overlay={overlay_text}, "
            f"delta={float(value['delta_to_default']):+.2f})"
        )

    lines: list[str] = []
    for track in state.get("tracks", []):
        lines.append(
            f"Track {track['track_id']} {track['name']}: {track['session_count']} sessions, "
            f"{track['ingested_count']} ingested"
        )
        for compound, values in sorted(track["compounds"].items()):
            parts = [f"{name}={prior_text(value)}" for name, value in values.items()]
            lines.append(f"  compound {compound}: " + ", ".join(parts))
        fuel = track["fuel_kg_per_lap"]
        lines.append(f"  fuel burn kg/lap: {prior_text(fuel)}")
        pit = ", ".join(
            f"{mode}={prior_text(value)}" for mode, value in track["pit_loss_ms"].items()
        )
        lines.append(f"  pit loss ms: {pit}")
        thermal = track["thermal"]
        lines.append(
            f"  thermal YAML window: {thermal['yaml_cold_c']:.1f}–{thermal['yaml_hot_c']:.1f} C"
        )
        for compound, (cold, hot) in sorted(thermal["yaml_by_compound_c"].items()):
            lines.append(f"  thermal YAML compound {compound}: {cold:.1f}–{hot:.1f} C")
        for compound, values in sorted(thermal["compounds"].items()):
            lines.append(
                f"  thermal compound {compound}: lo={prior_text(values['lo'])}, "
                f"hi={prior_text(values['hi'])}"
            )
        for name, value in sorted(track["energy"].items()):
            lines.append(f"  energy {name}: {prior_text(value)}")
        for name, value in sorted(track["battle_priors"].items()):
            lines.append(f"  battle {name}: {prior_text(value)}")
        for rule in track["driver_input_ack_neg_by_rule"]:
            lines.append(
                f"  feedback {rule['rule_id']}: ack={rule['ack_count']} neg={rule['neg_count']}"
            )
    tuned = state.get("tuned_cooldowns", [])
    lines.append(f"Tuned cooldown rules: {len(tuned)}")
    for item in tuned:
        lines.append(
            f"  {item['rule_id']}: x{item['multiplier']:.3f}, grades={item['grade_count']}"
        )
    return "\n".join(lines)
