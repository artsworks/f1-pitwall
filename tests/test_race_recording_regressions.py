from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.rules.engine import Candidate, RuleEngine
from pitwall.state.race import penalty_standing, relevant_rivals
from pitwall.state.session import CarLap, Participant, SessionState, Snapshot
from pitwall.store.db import Database


@dataclass
class _RecordingCar:
    car_position: int
    lap_distance: float
    total_distance: float
    delta_to_car_in_front_ms: int = 0
    delta_to_race_leader_ms: int = 0
    penalties: int = 0
    pit_status: int = 0
    result_status: int = 2


def test_recording_rival_gaps_use_distance_validation() -> None:
    speed = 7004 / 105
    player = _RecordingCar(5, 1000.0, 50000.0, delta_to_car_in_front_ms=1233)
    piastri = _RecordingCar(4, 1200.0, 50200.0, pit_status=1)
    assert relevant_rivals([player, piastri], 0, math.inf, (1000.0, 7004.0, 0.0), speed)[0] == -1

    player = _RecordingCar(2, 1000.0, 50000.0, delta_to_car_in_front_ms=1233)
    norris = _RecordingCar(1, 1709.0, 50709.0)
    assert relevant_rivals([player, norris], 0, math.inf, (1000.0, 7004.0, 0.0), speed)[0] == -1

    player = _RecordingCar(3, 1000.0, 50000.0, delta_to_car_in_front_ms=2970)
    ocon = _RecordingCar(2, 1198.0, 50198.0)
    assert relevant_rivals([player, ocon], 0, math.inf, (1000.0, 7004.0, 0.0), speed)[0] == 1


@pytest.mark.parametrize("russell_gap", [0, 65503])
def test_recording_finish_penalty_standing_rejects_russell_glitch(russell_gap: int) -> None:
    speed = 7004 / 105
    cars = [
        _RecordingCar(1, 1000.0, 100000.0, penalties=3),
        _RecordingCar(2, 1001.0, 100000.0, delta_to_race_leader_ms=russell_gap),
        _RecordingCar(3, 950.0, 99950.0, delta_to_race_leader_ms=1267),
    ]
    assert penalty_standing(cars, 0, 7004.0, speed_mps=speed)[0] == 3


def _session_rivals() -> tuple[SessionState, tuple[CarLap, ...]]:
    state = SessionState()
    state.session_type = 15
    state.track_length_m = 7004.0
    state.position = 3
    state.lap_distance = 3000.0
    state.delta_to_car_in_front_ms = 2970
    state.race_phase = "racing"
    state.participants = (
        Participant(name="HADJAR"),
        Participant(name="OCON"),
        Participant(name="PIASTRI"),
    )
    state._last_session_time = 811.0
    state._last_update["lap_data"] = 811.0
    cars = (
        CarLap(
            lap_distance=3000.0,
            total_distance=59032.0,
            car_position=3,
            result_status=2,
            delta_to_car_in_front_ms=2970,
        ),
        CarLap(
            lap_distance=3198.0,
            total_distance=59230.0,
            car_position=2,
            result_status=2,
        ),
        CarLap(
            lap_distance=2800.0,
            total_distance=58832.0,
            car_position=4,
            result_status=2,
        ),
    )
    state.cars_lap = cars
    return state, cars


def test_session_latches_validated_ahead_through_pit_lane() -> None:
    state, cars = _session_rivals()
    state.cars_lap = cars
    before = state.snapshot(811.0)
    assert before.rival_ahead_name == "OCON"
    assert before.gap_ahead_s == pytest.approx(2.97)

    state._last_session_time = 812.0
    state._last_update["lap_data"] = 812.0
    state.cars_lap = (
        cars[0],
        CarLap(
            lap_distance=3198.0,
            total_distance=59230.0,
            car_position=2,
            pit_status=1,
            result_status=2,
        ),
        CarLap(
            lap_distance=2800.0,
            total_distance=58832.0,
            car_position=2,
            pit_status=1,
            result_status=2,
        ),
    )
    state._cars_pitted_this_lap.update((1, 2))
    after = state.snapshot(812.0)
    assert after.rival_ahead_idx == 1
    assert after.rival_ahead_name == "OCON"
    assert after.rival_ahead_pitted
    assert after.rival_ahead_in_pit_lane
    assert after.gap_ahead_s == pytest.approx(2.97)


def test_session_ahead_pitted_requires_current_pit_status() -> None:
    state, _ = _session_rivals()
    state._pitted_lap_snapshot.add(1)

    snapshot = state.snapshot(811.0)

    assert snapshot.rival_ahead_idx == 1
    assert snapshot.cars[1].pit_status == 0
    assert snapshot.rival_ahead_pitted
    assert not snapshot.rival_ahead_in_pit_lane


def _rule_engine() -> RuleEngine:
    store = ConfigStore()
    settings = store.current()
    return RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=settings.resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )


def _finish_candidates(snapshot: Snapshot) -> list[Candidate]:
    return _rule_engine().evaluate(snapshot).candidates


def test_finish_rules_use_classified_position_and_gains() -> None:
    penalty = Snapshot(
        now=1252.0,
        session_kind="race",
        session_type=15,
        phase="finished",
        position=1,
        penalty_position=3,
        penalty_s=3,
        _ages={"lap_data": 0.1},
    )
    calls = _finish_candidates(penalty)
    finish_calls = [call for call in calls if call.rule.defn.id.startswith("finish_")]
    assert [call.rule.defn.id for call in finish_calls] == ["finish_penalty"]
    assert "P3" in finish_calls[0].text
    assert penalty.classified_position == 3
    assert penalty.classified_gained == 0
    assert penalty.classified_lost == 0

    podium = Snapshot(
        now=1252.0,
        session_kind="race",
        session_type=15,
        phase="finished",
        position=4,
        grid_position=3,
        penalty_position=3,
        _ages={"lap_data": 0.1},
    )
    podium_calls = _finish_candidates(podium)
    podium_call = next(call for call in podium_calls if call.rule.defn.id == "finish_podium")
    assert "P3" in podium_call.text
    assert podium.classified_gained == 0


def test_full_recording_replay_regressions(tmp_path: Path) -> None:
    recording = os.environ.get("PITWALL_RACE_RECORDING", "")
    path = Path(recording) if recording else None
    if path is None or not path.is_file():
        pytest.skip("PITWALL_RACE_RECORDING does not point to an existing recording")

    log = tmp_path / "decisions.jsonl"
    engine = build_engine(
        clock=VirtualClock(), sinks=[], db=Database(":memory:"), decision_log_path=log
    )
    started = time.perf_counter()
    _, calls = asyncio.run(run_replay(path, engine, None))
    elapsed = time.perf_counter() - started
    engine.dispatcher.log.flush()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line]

    relevant_rival_rows = [
        row
        for row in rows
        if row["rule_id"] == "rival_ahead_pitted" and row["outcome"] in {"queued", "fired"}
    ]
    assert all(
        "PIASTRI" not in row["text"] and "RUSSELL" not in row["text"] for row in relevant_rival_rows
    )
    fired_rival_rows = [
        row for row in rows if row["rule_id"] == "rival_ahead_pitted" and row["outcome"] == "fired"
    ]
    lap_eight_calls = [
        row
        for row in rows
        if row["rule_id"] == "rival_ahead_pitted" and row["outcome"] == "fired" and row["lap"] == 8
    ]
    regression_window_calls = [
        row for row in lap_eight_calls if 809.0 <= row["session_time"] <= 816.0
    ]
    assert all("OCON" in row["text"] for row in regression_window_calls)

    finish_call = next(call for call in calls if call.rule_id == "finish_penalty")
    assert "P3" in finish_call.text
    finish_row = next(
        row for row in rows if row["rule_id"] == "finish_penalty" and row["outcome"] == "fired"
    )
    assert finish_row["inputs"]["penalty_position"] == 3
    print(
        f"recording replay: {elapsed:.1f}s; rival_ahead_pitted calls="
        f"{[(row['session_time'], row['text']) for row in fired_rival_rows]}; "
        f"finish call={(finish_call.rule_id, finish_call.text)}"
    )
