from __future__ import annotations

import io
import json

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.models import InputSettings, PolicySettings, RuleDefModel
from pitwall.rules.engine import Candidate, Rule
from pitwall.state.session import Snapshot


class CollectSink:
    speaks_audio = False

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
        screen_only=rd.screen_only,
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


def _screen_sink() -> CollectSink:
    s = CollectSink()
    s.speaks_audio = True
    return s


def test_verbosity_budget_presets() -> None:
    d, sink, _ = _dispatcher(verbosity="normal", min_gap_s=0.0)
    cands = [_cand(f"r{i}") for i in range(6)]
    d.submit(cands, _snap(0.0))
    calls = d.drain(0.0)
    assert len(calls) == 4  # normal preset budget

    d, sink, _ = _dispatcher(verbosity="coach", min_gap_s=0.0)
    d.submit([_cand(f"r{i}") for i in range(10)], _snap(0.0))
    assert len(d.drain(0.0)) == 8

    # explicit calls_per_lap overrides the preset
    d, sink, _ = _dispatcher(verbosity="coach", calls_per_lap=2, min_gap_s=0.0)
    d.submit([_cand(f"r{i}") for i in range(6)], _snap(0.0))
    assert len(d.drain(0.0)) == 2


def test_p3_held_until_on_straight() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("p3", priority=3, text="info")], _snap(0.0))
    d.drain(0.0)  # snapshot default on_straight=False
    assert not sink.spoken
    # flip on_straight via a new snapshot on the next submit tick
    d.submit([], Snapshot(now=1.0, lap_num=1, on_straight=True))
    assert d.drain(1.0) and sink.spoken == ["info"]


def test_p3_dropped_at_deadline_off_straight() -> None:
    d, sink, _ = _dispatcher(deadlines_s={3: 1.0})
    d.submit([_cand("p3", priority=3, text="info")], _snap(0.0))
    d.drain(0.0)
    d.drain(2.0)  # past the 1 s P3 deadline while never on a straight
    assert not sink.spoken


def test_p3_straight_only_disabled() -> None:
    d, sink, _ = _dispatcher(p3_straight_only=False)
    d.submit([_cand("p3", priority=3, text="info")], _snap(0.0))
    d.drain(0.0)
    assert sink.spoken == ["info"]


def test_ack_then_say_again() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="box box")], _snap(0.0))
    d.drain(0.0)
    from pitwall.input.press import Press

    # ack with target -> logged, same-lap resubmission suppressed
    d.on_press(Press("ack", 0.5), _snap(0.5))
    d.submit([_cand("a", text="box box2")], _snap(1.0))
    assert d.drain(1.0) == []
    # ack with no live target -> say again re-speaks the last call
    d.on_press(Press("ack", 20.0), _snap(20.0))
    calls = d.drain(20.0)
    assert len(calls) == 1 and "say_again" in calls[0].tags


def test_say_again_ignores_stale_calls() -> None:
    from pitwall.input.press import Press

    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="pressures")], _snap(0.0))
    d.drain(0.0)
    # a press long after the call (e.g. menu buttons in the garage) repeats nothing
    d.on_press(Press("ack", 100.0), _snap(100.0))
    assert d.drain(100.0) == []


def test_negative_backoff_mutes_not_p1() -> None:
    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="call")], _snap(0.0, lap=1))
    d.drain(0.0)
    from pitwall.input.press import Press

    d.on_press(Press("neg", 1.0), _snap(1.0, lap=1))
    # muted for 3 laps
    for lap in (2, 3):
        d.submit([_cand("a", text="again")], _snap(10.0 + lap, lap=lap))
        assert d.drain(10.0 + lap) == []
    d.submit([_cand("a", text="again")], _snap(20.0, lap=4))
    assert len(d.drain(20.0)) == 1
    # P1 is never muted
    d2, _, _ = _dispatcher(min_gap_s=0.0)
    d2.submit([_cand("crit", priority=1, text="danger")], _snap(0.0, lap=1))
    d2.drain(0.0)
    d2.on_press(Press("neg", 1.0), _snap(1.0, lap=1))
    d2.submit([_cand("crit", priority=1, text="danger2")], _snap(2.0, lap=2))
    assert len(d2.drain(2.0)) == 1


def test_neg_no_target_sets_quiet_until_p1_speaks() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    from pitwall.input.press import Press

    d.on_press(Press("neg", 10.0), _snap(10.0))
    assert d.quiet_until is not None
    d.submit([_cand("a", text="info")], _snap(11.0))
    assert d.drain(11.0) == []
    d.submit([_cand("crit", priority=1, text="danger")], _snap(11.0))
    assert len(d.drain(11.0)) == 1  # P1 still speaks


def test_screen_only_not_spoken() -> None:
    sink = _screen_sink()  # acts like an audio sink
    buf = io.StringIO()
    d = Dispatcher(
        PolicySettings(),
        VirtualClock(),
        decision_log=DecisionLog(fp=buf),
        sinks=[sink],
    )
    d.submit([_cand("a", text="screen only", screen_only=True)], _snap(0.0))
    calls = d.drain(0.0)
    assert calls and calls[0].screen_only
    assert not sink.spoken  # audio sink skipped


def test_budget_resets_per_quali_run_on_same_lap() -> None:
    d, _, buf = _dispatcher(calls_per_lap=1, min_gap_s=0.0)
    for i, phase in enumerate(["flying", "flying", "garage", "out_lap"]):
        d.submit([_cand(f"r{i}")], Snapshot(now=float(i), lap_num=4, phase=phase))
        d.drain(float(i))
    fired = [r["rule_id"] for r in _log(buf) if r["outcome"] == "fired"]
    assert fired == ["r0", "r2", "r3"]


def test_press_gets_spoken_reply() -> None:
    from pitwall.input.press import Press

    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(spoken_replies=True)
    d.submit([_cand("a", text="box box")], _snap(0.0))
    d.drain(0.0)
    d.on_press(Press("ack", 1.0), _snap(1.0))
    calls = d.drain(1.0)
    assert len(calls) == 1 and calls[0].rule_id == "reply" and calls[0].text == "Copy."
    d.on_press(Press("neg", 2.0), _snap(2.0))
    assert [c.text for c in d.drain(2.0)] == ["Noted."]


def test_neg_without_target_announces_quiet_and_ack_ends_it() -> None:
    from pitwall.input.press import Press

    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(spoken_replies=True)
    d.on_press(Press("neg", 0.0), _snap(0.0))
    assert d.quiet_until is not None
    assert "going quiet" in d.drain(0.0)[0].text
    d.on_press(Press("ack", 10.0), _snap(10.0))
    assert d.quiet_until is None
    assert d.drain(10.0)[0].text == "Radio's back on."


def test_rule_reply_and_longer_response_window() -> None:
    from pitwall.input.press import Press

    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(spoken_replies=True)
    cand = _cand(
        "through", text="no need to push", response_window_s=30, on_neg=["Your call, push on"]
    )
    d.submit([cand], _snap(0.0))
    d.drain(0.0)
    d.on_press(Press("neg", 16.0), _snap(16.0))  # past the default 8 s window
    assert d.quiet_until is None
    assert [c.text for c in d.drain(16.0)] == ["Your call, push on"]
