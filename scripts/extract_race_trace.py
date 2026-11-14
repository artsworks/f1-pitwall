#!/usr/bin/env python3
"""Extract a lap-level trace from a real recording for the trace-seeded synthetic race.

The trace keeps session facts, one entry per player lap, the lap facts of the cars
directly ahead and behind at the line, and a fitted lap-time model. It holds no raw
telemetry and no driver names. Keep the output out of git until the repo owner
approves it.

Usage: uv run python scripts/extract_race_trace.py RECORDING [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from pitwall.net.recording import RecordingReader  # noqa: E402
from pitwall.protocol.header import PACKET_ID_OFFSET, PacketId, parse_header  # noqa: E402
from pitwall.protocol.packets import parse  # noqa: E402

WANTED = {
    int(PacketId.SESSION),
    int(PacketId.LAP_DATA),
    int(PacketId.CAR_STATUS),
    int(PacketId.CAR_DAMAGE),
    int(PacketId.CAR_TELEMETRY),
    int(PacketId.SESSION_HISTORY),
}
LAP_VALID_BIT = 0x01
GAP_BINS = 8
GAP_CAP_S = 59.9


def _new_lap(lap: int) -> dict[str, Any]:
    return {
        "lap": lap,
        "sc_status": 0,
        "pit_any": False,
        "pit_cars": set(),
        "ahead": {"pit": False},
        "behind": {"pit": False},
    }


def _gap_profile(samples: list[tuple[float, float]]) -> list[float]:
    start = samples[0][0]
    span = max(samples[-1][0] - start, 1e-6)
    bins: list[list[float]] = [[] for _ in range(GAP_BINS)]
    for when, gap in samples:
        bins[min(int((when - start) / span * GAP_BINS), GAP_BINS - 1)].append(gap)
    medians = [float(np.median(values)) if values else None for values in bins]
    known = next(value for value in medians if value is not None)
    profile: list[float] = []
    last = known
    for value in medians:
        last = value if value is not None else last
        profile.append(last)
    return profile


def read_facts(path: Path) -> tuple[int, dict[str, Any], dict[int, dict[str, Any]]]:
    laps: dict[int, dict[str, Any]] = {}
    history: dict[int, dict[str, Any]] = {}
    session: dict[str, Any] = {}
    player = 0
    current = 0
    with RecordingReader(path) as reader:
        uid = reader.header.session_uid
        for _t, payload in reader.records():
            pid = payload[PACKET_ID_OFFSET]
            if pid not in WANTED:
                continue
            pkt = parse(pid, payload, parse_header(payload))
            player = pkt.header.player_car_index
            if pid == int(PacketId.SESSION):
                for key in ("session_type", "track_id", "track_length", "total_laps"):
                    session.setdefault(key, int(getattr(pkt, key)))
                if current:
                    rec = laps[current]
                    rec.setdefault("weather", int(pkt.weather))
                    rec["sc_status"] = max(rec["sc_status"], int(pkt.safety_car_status))
            elif pid == int(PacketId.CAR_STATUS):
                car = pkt.cars[player]
                if current:
                    rec = laps[current]
                    rec.setdefault("compound", int(car.actual_tyre_compound))
                    rec.setdefault("tyre_age", int(car.tyres_age_laps))
                    rec.setdefault("fuel_kg", float(car.fuel_in_tank))
                    rec["ers_store_j"] = float(car.ers_store_energy)
                    for side in ("ahead", "behind"):
                        idx = rec[side].get("idx")
                        if idx is not None:
                            rec[side]["tyre_age"] = int(pkt.cars[idx].tyres_age_laps)
            elif pid == int(PacketId.CAR_DAMAGE):
                if current:
                    wear = pkt.cars[player].tyres_wear.as_tuple()
                    laps[current].setdefault("wear_pct", float(sum(wear) / len(wear)))
            elif pid == int(PacketId.CAR_TELEMETRY):
                if current:
                    inner = pkt.cars[player].tyres_inner_temperature.as_tuple()
                    rec = laps[current]
                    rec["inner_sum"] = rec.get("inner_sum", 0.0) + sum(inner) / len(inner)
                    rec["inner_n"] = rec.get("inner_n", 0) + 1
            elif pid == int(PacketId.SESSION_HISTORY):
                if pkt.car_idx == player:
                    for index in range(int(pkt.num_laps)):
                        entry = pkt.laps[index]
                        if entry.lap_time_ms:
                            history[index + 1] = {
                                "lap_time_ms": int(entry.lap_time_ms),
                                "s1_ms": int(entry.sector1_ms),
                                "s2_ms": int(entry.sector2_ms),
                                "valid": bool(int(entry.lap_valid_bit_flags) & LAP_VALID_BIT),
                            }
            else:
                cars = pkt.cars
                me = cars[player]
                lap = int(me.current_lap_num)
                when = pkt.header.session_time
                if lap != current:
                    if current:
                        laps[current]["completed_ms"] = int(me.last_lap_time_ms)
                    current = lap
                    laps.setdefault(lap, _new_lap(lap))["first_t"] = when
                    session.setdefault("grid_position", int(me.grid_position))
                rec = laps[lap]
                rec["last_t"] = when
                rec["position"] = int(me.car_position)
                rec["gap_ahead_s"] = float(me.delta_to_car_in_front_ms) / 1000
                rec.setdefault("gap_samples", []).append((when, min(rec["gap_ahead_s"], GAP_CAP_S)))
                if int(me.pit_status):
                    rec["pit_any"] = True
                for i in range(len(cars)):
                    if int(cars[i].pit_status):
                        rec["pit_cars"].add(i)
                by_position = {int(cars[i].car_position): i for i in range(len(cars))}
                ahead = by_position.get(int(me.car_position) - 1)
                behind = by_position.get(int(me.car_position) + 1)
                rec["ahead"]["idx"] = ahead
                rec["behind"]["idx"] = behind
                if behind is not None:
                    rec["gap_behind_s"] = float(cars[behind].delta_to_car_in_front_ms) / 1000
    for lap, rec in laps.items():
        rec["gap_profile_s"] = _gap_profile(rec.pop("gap_samples"))
        rec["tyre_inner_c"] = rec["inner_sum"] / rec["inner_n"] if rec.get("inner_n") else 0.0
        if lap in history:
            rec.update(history[lap])
        elif rec.get("completed_ms") is not None:
            rec["lap_time_ms"] = rec["completed_ms"]
            rec["valid"] = False
        else:
            rec["lap_time_ms"] = round((rec["last_t"] - rec["first_t"]) * 1000)
            rec["valid"] = False
            rec["time_estimated"] = True
        for side in ("ahead", "behind"):
            rec[side]["pit"] = rec[side].get("idx") in rec["pit_cars"]
    pit_laps = [lap for lap, rec in laps.items() if rec["pit_any"]]
    entry_lap = min(pit_laps, default=None)
    for lap, rec in laps.items():
        rec["pit"] = lap == entry_lap
    return uid, session, laps


def fit_noise(laps: list[dict[str, Any]]) -> dict[str, Any]:
    stint = 0
    previous_age: int | None = None
    for rec in laps:
        if previous_age is not None and rec["tyre_age"] < previous_age:
            stint += 1
        rec["stint"] = stint
        previous_age = rec["tyre_age"]
    stints = stint + 1
    out_laps = {rec["lap"] + 1 for rec in laps if rec["pit"]}
    green = [
        rec
        for rec in laps
        if rec["valid"]
        and not rec["sc_status"]
        and not rec["pit"]
        and rec["lap"] not in out_laps
        and rec["lap"] > 1
    ]
    design = np.zeros((len(green), 2 * stints))
    target = np.zeros(len(green))
    for row, rec in enumerate(green):
        design[row, rec["stint"]] = 1.0
        design[row, stints + rec["stint"]] = rec["tyre_age"]
        target[row] = rec["lap_time_ms"]
    coef = np.linalg.lstsq(design, target, rcond=None)[0]
    base = [float(value) for value in coef[:stints]]
    deg = [float(value) for value in coef[stints:]]
    for rec in laps:
        stint_index = rec["stint"]
        predicted = base[stint_index] + deg[stint_index] * rec["tyre_age"]
        rec["residual_ms"] = round(rec["lap_time_ms"] - predicted)
    stint_laps = [[rec["lap"] for rec in laps if rec["stint"] == s] for s in range(stints)]
    return {
        "model": "lap_ms = base_ms + deg_ms * tyre_age, per stint",
        "fuel_term": "not fitted: fuel and tyre age are collinear within a stint",
        "stints": [
            {
                "first_lap": min(stint_laps[s]),
                "last_lap": max(stint_laps[s]),
                "base_ms": round(base[s]),
                "deg_ms": round(deg[s], 2),
            }
            for s in range(stints)
        ],
        "green_laps_fitted": len(green),
        "residual_sd_ms": round(float(np.std([rec["residual_ms"] for rec in green]))),
    }


def build_trace(path: Path) -> dict[str, Any]:
    uid, session, laps = read_facts(path)
    ordered = [laps[lap] for lap in sorted(laps)]
    noise = fit_noise(ordered)
    player = []
    rival_ahead = []
    rival_behind = []
    for rec in ordered:
        estimated = {"time_estimated": True} if rec.get("time_estimated") else {}
        player.append(
            {
                "lap": rec["lap"],
                "lap_time_ms": rec["lap_time_ms"],
                **estimated,
                "s1_ms": rec.get("s1_ms"),
                "s2_ms": rec.get("s2_ms"),
                "compound": rec["compound"],
                "tyre_age": rec["tyre_age"],
                "wear_pct": round(rec["wear_pct"], 2),
                "fuel_kg": round(rec["fuel_kg"], 2),
                "ers_store_j": round(rec["ers_store_j"]),
                "position": rec["position"],
                "tyre_inner_c": round(rec["tyre_inner_c"], 1),
                "gap_ahead_s": round(rec["gap_ahead_s"], 3),
                "gap_ahead_profile_s": [round(gap, 3) for gap in rec["gap_profile_s"]],
                "gap_behind_s": round(rec.get("gap_behind_s", 0.0), 3),
                "pit": rec["pit"],
                "valid": rec["valid"],
                "weather": rec["weather"],
                "sc_status": rec["sc_status"],
                "residual_ms": rec["residual_ms"],
            }
        )
        rival_ahead.append(
            {
                "lap": rec["lap"],
                "pit": rec["ahead"]["pit"],
                "tyre_age": rec["ahead"].get("tyre_age"),
            }
        )
        rival_behind.append(
            {
                "lap": rec["lap"],
                "pit": rec["behind"]["pit"],
                "tyre_age": rec["behind"].get("tyre_age"),
            }
        )
    return {
        "source": {"session_uid": f"0x{uid:016x}", "recording": path.name},
        "session": {
            **session,
            "sc_laps": [rec["lap"] for rec in ordered if rec["sc_status"]],
        },
        "player": {"laps": player},
        "rival_ahead": rival_ahead,
        "rival_behind": rival_behind,
        "noise": noise,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    trace = build_trace(args.recording)
    uid_hex = trace["source"]["session_uid"][2:]
    out = args.out or ROOT / "scenarios" / "traces" / f"{uid_hex}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(trace, sort_keys=False, default_flow_style=None, width=120)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} laps={len(trace['player']['laps'])} bytes={out.stat().st_size}")


if __name__ == "__main__":
    main()
