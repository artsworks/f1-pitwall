from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.models import JevSettings, PolicySettings, RuleDefModel
from pitwall.rules.engine import Candidate, Rule
from pitwall.state.session import Snapshot
from pitwall.store.db import Database
from pitwall.voice.arbitrator import ArbCandidate, Ranking, heap_order, promote


class CollectSink:
    speaks_audio = False

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, call: Any) -> None:
        self.spoken.append(call.rule_id)

    def cancel(self, call_id: str) -> None:
        pass


class PickRule:
    """Promotes the first candidate with `rule_id`; records what it was asked."""

    model = "test/pick"

    def __init__(self, rule_id: str, gate: threading.Event | None = None) -> None:
        self.rule_id = rule_id
        self.gate = gate
        self.asked: list[list[ArbCandidate]] = []

    def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking:
        self.asked.append(list(candidates))
        if self.gate is not None:
            self.gate.wait(5)
        order = heap_order(candidates)
        for c in candidates:
            if c.rule_id == self.rule_id:
                return Ranking(promote(order, c.call_id), "jev", c.call_id, 0.9, 12.0, self.model)
        return Ranking(order, "jev_abstain", model=self.model)


class FakeWall:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _cand(rule_id: str, priority: int = 2) -> Candidate:
    rd = RuleDefModel.model_validate(
        {"id": rule_id, "priority": priority, "when": "True", "say": rule_id}
    )
    return Candidate(
        rule=Rule(rd),
        text=rule_id,
        priority=priority,
        tags=[],
        still_true=None,
        inputs={},
        trigger_t=0.0,
        screen_only=rd.screen_only,
    )


def _dispatcher(
    arbitrator: Any,
    *,
    inline: bool = True,
    wall: FakeWall | None = None,
    db: Database | None = None,
) -> tuple[Dispatcher, CollectSink, io.StringIO]:
    sink = CollectSink()
    buf = io.StringIO()
    d = Dispatcher(
        PolicySettings.model_validate({"p3_straight_only": False}),
        VirtualClock(),
        decision_log=DecisionLog(fp=buf, db=db, session_uid_source=lambda: 42),
        sinks=[sink],
        arbitrator=arbitrator,
        arbitration=JevSettings(enabled=True, arbitrate=True),
        inline_arbitration=inline,
        wall=wall or FakeWall(),
    )
    return d, sink, buf


def _snap(now: float) -> Snapshot:
    return Snapshot(now=now, lap_num=3, session_time=100.0 + now, session_uid=42)


def _log(buf: io.StringIO) -> list[dict[str, Any]]:
    buf.seek(0)
    return [json.loads(line) for line in buf if line.strip()]


def _drain_all(d: Dispatcher, until: float) -> None:
    t = 0.0
    while t <= until:
        d.drain(t)
        t += 0.5


def test_pick_reorders_p2_calls() -> None:
    arb = PickRule("pit_window")
    d, sink, _ = _dispatcher(arb)
    d.submit([_cand("gap_ahead"), _cand("pit_window")], _snap(0.0))
    _drain_all(d, 20.0)
    assert sink.spoken == ["pit_window", "gap_ahead"]


def test_p1_never_sent_and_never_reordered() -> None:
    arb = PickRule("gap_ahead")
    d, sink, _ = _dispatcher(arb)
    d.submit([_cand("flag", 1), _cand("pit_window"), _cand("gap_ahead")], _snap(0.0))
    _drain_all(d, 20.0)
    assert arb.asked
    assert all(c.priority != 1 for asked in arb.asked for c in asked)
    assert sink.spoken[0] == "flag"
    assert sink.spoken[1:] == ["gap_ahead", "pit_window"]


def test_single_p2_with_p1_is_not_arbitrated() -> None:
    arb = PickRule("x")
    d, sink, _ = _dispatcher(arb)
    d.submit([_cand("flag", 1), _cand("pit_window")], _snap(0.0))
    _drain_all(d, 20.0)
    assert arb.asked == []
    assert sink.spoken == ["flag", "pit_window"]


def test_prefetch_does_not_block_submit_or_p1() -> None:
    gate = threading.Event()
    arb = PickRule("pit_window", gate=gate)
    wall = FakeWall()
    d, sink, _ = _dispatcher(arb, inline=False, wall=wall)
    t0 = time.monotonic()
    d.submit([_cand("gap_ahead"), _cand("pit_window"), _cand("flag", 1)], _snap(0.0))
    assert time.monotonic() - t0 < 1.0
    calls = d.drain(0.0)
    assert [c.rule_id for c in calls] == ["flag"]  # P2s wait for the answer, P1 does not
    assert d.drain(0.1) == []
    gate.set()
    for _ in range(100):
        if d._arb_pending is not None and d._arb_pending.future.done():  # noqa: SLF001
            break
        time.sleep(0.01)
    _drain_all(d, 20.0)
    assert sink.spoken == ["flag", "pit_window", "gap_ahead"]


def test_slow_answer_times_out_to_heap_order() -> None:
    gate = threading.Event()
    arb = PickRule("pit_window", gate=gate)
    wall = FakeWall()
    d, sink, buf = _dispatcher(arb, inline=False, wall=wall)
    d.submit([_cand("gap_ahead"), _cand("pit_window")], _snap(0.0))
    assert d.drain(0.0) == []
    wall.t = 0.5  # past timeout_ms = 300
    _drain_all(d, 20.0)
    gate.set()
    assert sink.spoken == ["gap_ahead", "pit_window"]
    arbitrated = [r for r in _log(buf) if r["outcome"] == "arbitrated"]
    assert arbitrated[0]["arbitrated_by"] == "jev_timeout"
    assert arbitrated[0]["arb_latency_ms"] >= 300


def test_failing_arbitrator_keeps_heap_order() -> None:
    class Boom:
        model = "test/boom"

        def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking:
            raise RuntimeError("down")

    d, sink, buf = _dispatcher(Boom())
    d.submit([_cand("gap_ahead"), _cand("pit_window")], _snap(0.0))
    _drain_all(d, 20.0)
    assert sink.spoken == ["gap_ahead", "pit_window"]
    assert [r["arbitrated_by"] for r in _log(buf) if r["outcome"] == "arbitrated"] == ["jev_error"]


def test_pick_is_recorded_on_log_and_db() -> None:
    db = Database(":memory:")
    arb = PickRule("pit_window")
    d, _, buf = _dispatcher(arb, db=db)
    d.submit([_cand("gap_ahead"), _cand("pit_window")], _snap(0.0))
    _drain_all(d, 20.0)
    log = _log(buf)
    arbitrated = [r for r in log if r["outcome"] == "arbitrated"]
    assert len(arbitrated) == 1
    rec = arbitrated[0]
    assert rec["arbitrated_by"] == "jev"
    assert rec["arb_order"] == [1, 0]
    assert rec["arb_model"] == "test/pick"
    assert rec["arb_confidence"] == 0.9
    assert rec["arb_key"].startswith("42/")
    assert [c["rule_id"] for c in rec["candidates"]] == ["gap_ahead", "pit_window"]
    assert rec["digest"]["lap"] == 3
    fired = [r for r in log if r["outcome"] == "fired"]
    assert {r["arbitrated_by"] for r in fired} == {"jev"}
    assert {r["arb_key"] for r in fired} == {rec["arb_key"]}

    rows = db.calls_for_session(42)
    fired_rows = [r for r in rows if r["outcome"] == "fired"]
    assert [r["rule_id"] for r in fired_rows] == ["pit_window", "gap_ahead"]
    assert {r["arbitrated_by"] for r in fired_rows} == {"jev"}
    assert {r["arb_model"] for r in fired_rows} == {"test/pick"}
    assert fired_rows[0]["arb_pick"] == rec["arb_pick"]
    assert fired_rows[0]["arb_confidence"] == 0.9
    (row,) = db.arbitrations(42)
    assert row["arb_key"] == rec["arb_key"]
    assert row["arb_order"] == [1, 0]


def test_no_arbitrator_logs_nothing_new() -> None:
    d, sink, buf = _dispatcher(None)
    d.submit([_cand("gap_ahead"), _cand("pit_window")], _snap(0.0))
    _drain_all(d, 20.0)
    assert sink.spoken == ["gap_ahead", "pit_window"]
    log = _log(buf)
    assert not [r for r in log if r["outcome"] == "arbitrated"]
    assert all("arbitrated_by" not in r for r in log)
