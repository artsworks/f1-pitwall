"""Trace-seeded synthetic race: per-lap overrides and bench parity with baseline.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pitwall.config.loader import ConfigStore
from pitwall.learning.bench import parse_scenario, run_scenario
from pitwall.protocol.header import PacketId, parse_header
from pitwall.protocol.packets import parse

from .race_synth import race_stream, spec_from_trace
from .synth import write_packet_stream

ROOT = Path(__file__).resolve().parents[1]
TRACE_PATH = ROOT / "scenarios" / "traces" / "2188302d49bba3bb.yaml"
BRAZIL_SCENARIOS = ("brazil-race-real", "brazil-sc-early", "brazil-worn-tyres", "brazil-penalty")

TINY_TRACE: dict[str, Any] = {
    "source": {"session_uid": "0x2188302d49bba3bb"},
    "session": {
        "session_type": 15,
        "track_id": 16,
        "track_length": 4294,
        "total_laps": 3,
        "grid_position": 2,
    },
    "player": {
        "laps": [
            {
                "lap": 1,
                "lap_time_ms": 81_000,
                "compound": 18,
                "tyre_age": 0,
                "wear_pct": 0.0,
                "fuel_kg": 18.5,
                "ers_store_j": 2_000_000.0,
                "position": 2,
                "tyre_inner_c": 95.0,
                "gap_ahead_s": 1.2,
                "gap_ahead_profile_s": [1.2] * 8,
                "gap_behind_s": 3.4,
                "pit": False,
                "valid": True,
                "weather": 0,
                "sc_status": 0,
            },
            {
                "lap": 2,
                "lap_time_ms": 74_000,
                "compound": 17,
                "tyre_age": 1,
                "wear_pct": 3.5,
                "fuel_kg": 17.25,
                "ers_store_j": 1_500_000.0,
                "position": 2,
                "tyre_inner_c": 98.0,
                "gap_ahead_s": 0.8,
                "gap_ahead_profile_s": [0.8] * 8,
                "gap_behind_s": 4.0,
                "pit": False,
                "valid": True,
                "weather": 1,
                "sc_status": 0,
            },
            {
                "lap": 3,
                "lap_time_ms": 72_000,
                "compound": 17,
                "tyre_age": 2,
                "wear_pct": 7.25,
                "fuel_kg": 16.0,
                "ers_store_j": 900_000.0,
                "position": 2,
                "tyre_inner_c": 101.0,
                "gap_ahead_s": 0.5,
                "gap_ahead_profile_s": [0.5] * 8,
                "gap_behind_s": 4.5,
                "pit": False,
                "valid": True,
                "weather": 1,
                "sc_status": 0,
            },
        ]
    },
    "rival_ahead": [{"lap": lap, "pit": False, "tyre_age": lap} for lap in (1, 2, 3)],
    "rival_behind": [{"lap": lap, "pit": False, "tyre_age": lap} for lap in (1, 2, 3)],
    "noise": {
        "stints": [{"first_lap": 1, "last_lap": 3, "base_ms": 73_000, "deg_ms": 100.0}],
        "residual_sd_ms": 0,
    },
}


def _lap_samples(stream: list[tuple[float, bytes]]) -> dict[str, Any]:
    lap = 0
    first: dict[tuple[str, int], Any] = {}
    last_lap_ms: dict[int, int] = {}
    distances: list[float] = []
    track_length = 0
    for _t, payload in stream:
        header = parse_header(payload)
        packet = parse(header.packet_id, payload, header)
        if header.packet_id == PacketId.SESSION:
            track_length = packet.track_length
        elif header.packet_id == PacketId.LAP_DATA:
            car = packet.cars[0]
            if car.current_lap_num != lap:
                if lap:
                    last_lap_ms[lap] = car.last_lap_time_ms
                lap = car.current_lap_num
            distances.append(car.lap_distance)
        elif header.packet_id == PacketId.CAR_STATUS:
            car = packet.cars[0]
            first.setdefault(("fuel", lap), car.fuel_in_tank)
            first.setdefault(("ers", lap), car.ers_store_energy)
        elif header.packet_id == PacketId.CAR_DAMAGE:
            first.setdefault(("wear", lap), packet.cars[0].tyres_wear.as_tuple()[0])
    return {
        "track_length": track_length,
        "last_lap_ms": last_lap_ms,
        "first": first,
        "max_distance": max(distances),
    }


def test_trace_overrides_reach_the_stream():
    spec = spec_from_trace(TINY_TRACE, dt=1.0)
    samples = _lap_samples(race_stream(spec))
    laps = TINY_TRACE["player"]["laps"]
    assert samples["track_length"] == 4294
    assert 0.9 * 4294 < samples["max_distance"] < 4294
    assert samples["last_lap_ms"] == {1: 81_000, 2: 74_000}
    for lap in laps:
        number = lap["lap"]
        assert samples["first"][("fuel", number)] == pytest.approx(lap["fuel_kg"])
        assert samples["first"][("ers", number)] == pytest.approx(lap["ers_store_j"])
        assert samples["first"][("wear", number)] == pytest.approx(lap["wear_pct"])


@pytest.mark.slow
@pytest.mark.replay
@pytest.mark.parametrize("scenario_id", BRAZIL_SCENARIOS)
def test_trace_race_matches_baseline(tmp_path, scenario_id):
    if not TRACE_PATH.exists():
        pytest.skip(f"{TRACE_PATH} is not present")
    trace = yaml.safe_load(TRACE_PATH.read_text(encoding="utf-8"))
    spec = spec_from_trace(trace, noise="replay")
    recording = write_packet_stream(
        tmp_path / "trace.f1bin", race_stream(spec), session_uid=spec.session_uid
    )
    scenario = parse_scenario(ROOT / "scenarios" / f"{scenario_id}.yaml")
    row, _outcomes = run_scenario(scenario, recording, ConfigStore().current())
    assert row["result"] != "error", row.get("reason")
    baseline = json.loads((ROOT / "scenarios" / "baseline.json").read_text(encoding="utf-8"))
    accepted = baseline["scenarios"][scenario_id]["checks"]
    synthetic = row["checks"]
    assert [(c["type"], c["rules"], c["laps"]) for c in synthetic] == [
        (c["type"], c["rules"], c["laps"]) for c in accepted
    ]
    mismatches = [
        f"{want['type']} {','.join(want['rules'])} laps {want['laps']}: "
        f"baseline ok={want['ok']}, synthetic ok={got['ok']}, found={got['found']}"
        for got, want in zip(synthetic, accepted, strict=True)
        if got["ok"] != want["ok"]
    ]
    assert not mismatches, f"{scenario_id}: " + "; ".join(mismatches)
