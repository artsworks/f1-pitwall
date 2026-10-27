"""Fit mismatch reporting across pit stops under green, SC, and VSC."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.model.deg import DegFit
from pitwall.server.app import strategy_payload
from pitwall.state.session import Snapshot
from pitwall.store.db import Database
from pitwall.strategy.pitwindow import NO_PLAN

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream

_EXPECTED = "SOFT"
_WRONG_VISUAL = 18
_RIGHT_VISUAL = 16
_LAP = 7

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
        "drive_through": RaceSpec(
            laps=12,
            player_pit_lap=6,
            player_pit_drive_through=True,
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


@pytest.mark.slow
@pytest.mark.replay
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


@pytest.mark.slow
@pytest.mark.replay
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


@pytest.mark.slow
@pytest.mark.replay
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


@pytest.mark.slow
@pytest.mark.replay
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


def _plan_engine(tmp_path: Path) -> Any:
    return build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=Database(":memory:"),
        decision_log_path=tmp_path / "d.jsonl",
    )


def _plan_snapshot(t: float, pit_status: int, tyre_visual: int, phase: str = "in_lap") -> Snapshot:
    return dataclasses.replace(
        Snapshot(now=t),
        session_kind="race",
        session_type=15,
        race_phase=phase,
        laps_remaining=20,
        lap_num=_LAP,
        session_time=t,
        pit_status=pit_status,
        num_pit_stops=int(phase == "out_lap"),
        tyre_visual=tyre_visual,
        active_plan="A",
        plan_next_compound="soft",
    )


def _fit_mismatch(engine: Any, snap: Snapshot) -> dict[str, str] | None:
    prior = DegFit(90_000.0, 80.0, 30.0, 10, 100.0, 0.9, "fit")
    result = engine._plans(snap, prior, NO_PLAN)
    payload = strategy_payload(result)
    assert payload is not None
    return payload["fit_mismatch"]


def test_rewind_to_before_pit_entry_does_not_latch_mismatch(tmp_path: Path) -> None:
    engine = _plan_engine(tmp_path)
    assert _fit_mismatch(engine, _plan_snapshot(100, 0, 17)) is None
    assert _fit_mismatch(engine, _plan_snapshot(110, 1, 17)) is None
    for cb in engine.state.rewind_listeners:
        cb(95)
    assert _fit_mismatch(engine, _plan_snapshot(96, 0, 17)) is None
    assert _fit_mismatch(engine, _plan_snapshot(97, 0, 17)) is None


def test_rewind_after_pit_exit_keeps_mismatch(tmp_path: Path) -> None:
    engine = _plan_engine(tmp_path)
    assert _fit_mismatch(engine, _plan_snapshot(100, 0, 17)) is None
    assert _fit_mismatch(engine, _plan_snapshot(110, 1, 17)) is None
    expected = {"plan": "A", "expected": "SOFT", "fitted": "HARD"}
    assert _fit_mismatch(engine, _plan_snapshot(130, 0, 18, "out_lap")) == expected
    assert _fit_mismatch(engine, _plan_snapshot(140, 0, 18, "out_lap")) == expected
    for cb in engine.state.rewind_listeners:
        cb(135)
    assert _fit_mismatch(engine, _plan_snapshot(136, 0, 18, "out_lap")) == expected
    assert _fit_mismatch(engine, _plan_snapshot(137, 0, 18, "out_lap")) == expected


def test_rewind_before_pit_entry_clears_mismatch_after_exit(tmp_path: Path) -> None:
    engine = _plan_engine(tmp_path)
    assert _fit_mismatch(engine, _plan_snapshot(100, 0, 17)) is None
    assert _fit_mismatch(engine, _plan_snapshot(110, 1, 17)) is None
    expected = {"plan": "A", "expected": "SOFT", "fitted": "HARD"}
    assert _fit_mismatch(engine, _plan_snapshot(130, 0, 18, "out_lap")) == expected
    assert _fit_mismatch(engine, _plan_snapshot(140, 0, 18, "out_lap")) == expected
    for cb in engine.state.rewind_listeners:
        cb(95)
    assert _fit_mismatch(engine, _plan_snapshot(96, 0, 17)) is None
    assert _fit_mismatch(engine, _plan_snapshot(97, 0, 17)) is None


def test_drive_through_does_not_warn(replays: dict[str, list[ReplayTick]]) -> None:
    ticks = replays["drive_through"]
    assert any(snapshot.pit_status != 0 for snapshot, _ in ticks)
    assert any(snapshot.plan_next_compound.upper() not in ("", "MEDIUM") for snapshot, _ in ticks)
    assert all(payload["fit_mismatch"] is None for _, payload in ticks)
