from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.models import JevSettings, PolicySettings
from pitwall.net.replay import load_recorded_picks
from pitwall.state.session import Snapshot
from pitwall.store.db import Database
from pitwall.voice.arbitrator import RecordedArbitrator
from .test_dispatcher_arbitration import CollectSink, PickRule, _cand, _snap


def _session(arbitrator: Any, log: DecisionLog) -> list[str]:
    sink = CollectSink()
    d = Dispatcher(
        PolicySettings.model_validate({"p3_straight_only": False}),
        VirtualClock(),
        decision_log=log,
        sinks=[sink],
        arbitrator=arbitrator,
        arbitration=JevSettings(enabled=True, arbitrate=True),
        inline_arbitration=True,
    )
    d.submit([_cand("gap_ahead"), _cand("deg_report", 3), _cand("pit_window")], _snap(0.0))
    t = 0.0
    while t <= 30.0:
        d.drain(t)
        t += 0.5
    next_lap = Snapshot(now=40.0, lap_num=4, session_time=140.0, session_uid=42)
    d.submit([_cand("fuel_delta", 3), _cand("lap_delta", 3)], next_lap)
    while t <= 70.0:
        d.drain(t)
        t += 0.5
    return sink.spoken


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_recorded_pick_round_trips_through_jsonl(tmp_path: Path) -> None:
    live_log = tmp_path / "race.decisions.jsonl"
    with live_log.open("w") as fp:
        live = _session(PickRule("pit_window"), DecisionLog(fp=fp))
    assert live[:3] == ["pit_window", "gap_ahead", "deg_report"]

    recorded = RecordedArbitrator(load_recorded_picks([live_log]))
    assert len(recorded.picks) == 2
    replay_log = tmp_path / "replay.jsonl"
    with replay_log.open("w") as fp:
        replayed = _session(recorded, DecisionLog(fp=fp))
    assert replayed == live
    assert recorded.hits == 2 and recorded.misses == 0

    def fired(path: Path) -> list[tuple[str, str]]:
        return [
            (r["rule_id"], r["arbitrated_by"]) for r in _records(path) if r["outcome"] == "fired"
        ]

    assert fired(replay_log) == fired(live_log)
    live_arb = [r for r in _records(live_log) if r["outcome"] == "arbitrated"]
    replay_arb = [r for r in _records(replay_log) if r["outcome"] == "arbitrated"]
    assert [(r["arb_key"], r["arb_order"]) for r in replay_arb] == [
        (r["arb_key"], r["arb_order"]) for r in live_arb
    ]
    assert {r["arb_model"] for r in replay_arb} == {"test/pick"}


def test_recorded_pick_round_trips_through_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "pitwall.db"
    db = Database(db_path)
    live = _session(PickRule("pit_window"), DecisionLog(db=db, session_uid_source=lambda: 42))
    recorded = RecordedArbitrator(load_recorded_picks([db_path]))
    assert _session(recorded, DecisionLog()) == live


def test_unrecorded_decision_points_replay_in_heap_order(tmp_path: Path) -> None:
    heap = _session(None, DecisionLog())
    recorded = RecordedArbitrator(load_recorded_picks([tmp_path / "missing.jsonl"]))
    assert _session(recorded, DecisionLog()) == heap
    assert recorded.hits == 0 and recorded.misses == 2
