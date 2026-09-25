from __future__ import annotations

import io
import json

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.models import PolicySettings, RuleDefModel
from pitwall.rules.engine import Candidate, Rule
from pitwall.state.session import Snapshot


class CollectSink:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.cancelled: list[str] = []

    def speak(self, call) -> None:  # type: ignore[no-untyped-def]
        self.spoken.append(call.text)

    def cancel(self, call_id: str) -> None:
        self.cancelled.append(call_id)


def _dispatcher(**policy_kw: object) -> tuple[Dispatcher, CollectSink, io.StringIO]:
    sink = CollectSink()
    buf = io.StringIO()
    d = Dispatcher(
        PolicySettings.model_validate(policy_kw),
        VirtualClock(),
        decision_log=DecisionLog(fp=buf),
        sinks=[sink],
    )
    return d, sink, buf


def _cand(rule_id: str, priority: int = 2, text: str | None = None, **defn: object) -> Candidate:
    rd = RuleDefModel.model_validate(
        {
            "id": rule_id,
            "priority": priority,
            "when": "True",
            "say": text or rule_id,
            **defn,
        }
    )
    rule = Rule(rd)
    return Candidate(
        rule=rule,
        text=text or rule_id,
        priority=priority,
        tags=[],
        still_true=None,
        inputs={},
        trigger_t=0.0,
    )


def _snap(now: float, lap: int = 1) -> Snapshot:
    return Snapshot(now=now, lap_num=lap)


def _log(buf: io.StringIO) -> list[dict]:
    buf.seek(0)
    return [json.loads(line) for line in buf if line.strip()]


def test_quiet_and_verbosity() -> None:
    d, sink, _ = _dispatcher(quiet=True)
    d.submit([_cand("a")], _snap(0.0))
    assert d.drain(0.0) == []
    assert not sink.spoken

    d, sink, _ = _dispatcher(verbosity="critical")
    d.submit([_cand("p2", priority=2), _cand("p1", priority=1)], _snap(0.0))
    calls = d.drain(0.0)
    assert [c.rule_id for c in calls] == ["p1"]


def test_cooldown_and_dedupe() -> None:
    d, sink, _ = _dispatcher()
    d.submit([_cand("a", cooldown_s=30, text="hi")], _snap(0.0))
    d.drain(0.0)
    d.submit([_cand("a", cooldown_s=30, text="hi")], _snap(10.0))
    assert d.drain(10.0) == []  # cooldown
    d.submit([_cand("b", text="hi")], _snap(10.0))
    assert d.drain(10.0) == []  # same text inside dedupe window
    d.submit([_cand("a", cooldown_s=30, text="hi")], _snap(31.0))
    assert len(d.drain(31.0)) == 1


def test_budget_and_p1_exempt() -> None:
    d, sink, _ = _dispatcher(calls_per_lap=1, min_gap_s=0.0)
    d.submit([_cand("a"), _cand("b")], _snap(0.0))
    d.submit([_cand("c", priority=1)], _snap(0.0))
    calls = d.drain(0.0)
    assert [c.rule_id for c in calls] == ["c", "a"]  # P1 preempts the budget


def test_min_gap() -> None:
    d, sink, _ = _dispatcher(min_gap_s=5.0)
    d.submit([_cand("a")], _snap(0.0))
    d.drain(0.0)
    d.submit([_cand("b")], _snap(1.0))
    assert d.drain(1.0) == []
    d.submit([_cand("c")], _snap(6.0))
    assert len(d.drain(6.0)) == 1


def test_deadline_drop() -> None:
    d, sink, _ = _dispatcher(deadlines_s={1: 5.0, 2: 0.5, 3: 1.5})
    d.submit([_cand("a")], _snap(0.0))
    assert d.drain(2.0) == []  # 2 s past a 0.5 s P2 deadline
    assert not sink.spoken


def test_revalidation() -> None:
    cand = _cand("a")
    cand.still_true = lambda snap: snap.lap_num > 0  # type: ignore[method-assign]
    d, sink, _ = _dispatcher()
    d.submit([cand], _snap(0.0, lap=1))
    assert len(d.drain(0.0)) == 1

    cand2 = _cand("b")
    cand2.still_true = lambda snap: False  # type: ignore[method-assign]
    d.submit([cand2], _snap(1.0, lap=1))
    assert d.drain(1.0) == []


def test_log_records_outcomes() -> None:
    d, sink, buf = _dispatcher()
    d.submit([_cand("a"), _cand("b", priority=1)], _snap(0.0))
    d.drain(0.0)
    records = _log(buf)
    outcomes = {(r["rule_id"], r["outcome"], r["suppressed_by"]) for r in records}
    assert ("a", "queued", None) in outcomes
    assert ("a", "fired", None) in outcomes
    assert ("b", "fired", None) in outcomes
