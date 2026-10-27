"""Fit mismatch reporting across pit stops under green, SC, and VSC."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.server.app import strategy_payload
from pitwall.state.session import Snapshot
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream

pytestmark = [pytest.mark.slow, pytest.mark.replay]

_EXPECTED = "SOFT"
_WRONG_VISUAL = 18
_RIGHT_VISUAL = 16

ReplayTick = tuple[Snapshot, dict[str, Any]]


def _replay(tmp: Path, spec: RaceSpec) -> list[ReplayTick]:
    path = write_packet_stream(tmp / "race.f1bin", race_stream(spec))
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=Database(":memory:"),
        decision_log_path=tmp / "decisions.jsonl",
    )
    snapshots: list[Snapshot] = []
    original = engine._plans

    def capture(snap: Snapshot, cur: Any, pit: Any) -> Snapshot:
        result = original(snap, cur, pit)
        snapshots.append(result)
        return result

    engine._plans = capture
    asyncio.run(run_replay(path, engine, None))
    ticks: list[ReplayTick] = []
    for snapshot in snapshots:
        payload = strategy_payload(snapshot)
        assert payload is not None
        ticks.append((snapshot, payload))
    return ticks


@pytest.fixture(scope="module")
def replays(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[ReplayTick]]:
    scenarios = {
        "vsc_wrong": RaceSpec(
            laps=12,
            sc_laps=(5, 8),
            vsc=True,
            player_pit_lap=6,
            compound_after_stop=_WRONG_VISUAL,
        ),
        "sc_wrong": RaceSpec(
            laps=12,
            sc_laps=(5, 8),
            vsc=False,
            player_pit_lap=6,
            compound_after_stop=_WRONG_VISUAL,
        ),
        "vsc_right": RaceSpec(
            laps=12,
            sc_laps=(5, 8),
            vsc=True,
            player_pit_lap=6,
            compound_after_stop=_RIGHT_VISUAL,
        ),
        "green_wrong": RaceSpec(
            laps=12,
            player_pit_lap=6,
            compound_after_stop=_WRONG_VISUAL,
        ),
    }
    return {name: _replay(tmp_path_factory.mktemp(name), spec) for name, spec in scenarios.items()}


def _assert_no_mismatch_before_pit(ticks: list[ReplayTick]) -> None:
    first_pit = next(i for i, (snapshot, _) in enumerate(ticks) if snapshot.pit_status != 0)
    assert all(payload["fit_mismatch"] is None for _, payload in ticks[:first_pit])


def test_vsc_wrong_tyre_uses_latched_plan(replays: dict[str, list[ReplayTick]]) -> None:
    ticks = replays["vsc_wrong"]
    _assert_no_mismatch_before_pit(ticks)
    assert any(
        snapshot.lap_num == 6
        and snapshot.pit_status != 0
        and snapshot.active_plan == "A"
        and snapshot.plan_next_compound.upper() == _EXPECTED
        for snapshot, _ in ticks
    )
    assert any(
        snapshot.lap_num == 7
        and snapshot.race_phase == "vsc"
        and snapshot.stop_plan == "A"
        and snapshot.stop_expected_compound == _EXPECTED
        and payload["fit_mismatch"] == {"plan": "A", "expected": _EXPECTED, "fitted": "HARD"}
        for snapshot, payload in ticks
    )
    lap_eight = [(snapshot, payload) for snapshot, payload in ticks if snapshot.lap_num == 8]
    assert lap_eight
    assert all(
        snapshot.race_phase == "vsc" and payload["fit_mismatch"] is None
        for snapshot, payload in lap_eight
    )


def test_sc_wrong_tyre_uses_latched_plan(replays: dict[str, list[ReplayTick]]) -> None:
    ticks = replays["sc_wrong"]
    _assert_no_mismatch_before_pit(ticks)
    assert any(
        snapshot.lap_num == 7
        and snapshot.race_phase == "sc"
        and snapshot.stop_expected_compound == _EXPECTED
        and payload["fit_mismatch"] == {"plan": "A", "expected": _EXPECTED, "fitted": "HARD"}
        for snapshot, payload in ticks
    )
    lap_eight = [(snapshot, payload) for snapshot, payload in ticks if snapshot.lap_num == 8]
    assert lap_eight
    assert all(
        snapshot.race_phase == "sc" and payload["fit_mismatch"] is None
        for snapshot, payload in lap_eight
    )


def test_vsc_right_tyre_does_not_warn(replays: dict[str, list[ReplayTick]]) -> None:
    ticks = replays["vsc_right"]
    latched = [
        snapshot
        for snapshot, _ in ticks
        if snapshot.lap_num == 7 and snapshot.stop_expected_compound
    ]
    assert latched and all(snapshot.stop_expected_compound == _EXPECTED for snapshot in latched)
    assert all(snapshot.tyre_visual == _RIGHT_VISUAL for snapshot in latched)
    assert all(payload["fit_mismatch"] is None for _, payload in ticks)


def test_green_wrong_tyre_warns_only_on_out_lap(
    replays: dict[str, list[ReplayTick]],
) -> None:
    ticks = replays["green_wrong"]
    _assert_no_mismatch_before_pit(ticks)
    assert any(
        snapshot.lap_num == 7
        and snapshot.race_phase == "out_lap"
        and payload["fit_mismatch"] == {"plan": "A", "expected": _EXPECTED, "fitted": "HARD"}
        for snapshot, payload in ticks
    )
    assert all(
        payload["fit_mismatch"] is None for snapshot, payload in ticks if snapshot.lap_num != 7
    )
