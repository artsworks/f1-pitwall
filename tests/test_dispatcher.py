from __future__ import annotations

import io
import json

import pytest

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import InputSettings, PolicySettings, RuleDefModel
from pitwall.digest import call_quality
from pitwall.rules.engine import Candidate, Rule
from pitwall.state.session import Snapshot
from pitwall.store.db import Database


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
        tags=list(rd.tags),
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


def test_p3_deadline_starts_after_3_second_p2_speech() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    assert d.policy.deadlines_s == {1: 5.0, 2: 3.0, 3: 1.5}
    p2_text = "p" * 45
    d.submit([_cand("p2", priority=2, text=p2_text)], Snapshot(now=0.0))
    assert d.drain(0.0)[0].text == p2_text
    speech_end = d._spoken_calls[-1][1]
    assert speech_end == pytest.approx(3.0)

    d.submit([_cand("p3", priority=3, text="info")], Snapshot(now=0.5, on_straight=True))
    assert d.drain(0.5) == []
    held = d._queue[0].call
    assert held.ready_t == pytest.approx(speech_end)
    assert [call.rule_id for call in d.drain(speech_end)] == ["p3"]
    assert sink.spoken == [p2_text, "info"]


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


def test_stale_queued_call_in_group_is_superseded() -> None:
    state = {"old_holds": True}
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: state["old_holds"]
    new = _cand("new", priority=1, conflict_group="slow_car")
    new.current = lambda snap: True
    d, sink, buf = _dispatcher()

    d.submit([old], _snap(0.0))
    state["old_holds"] = False
    d.submit([new], _snap(1.0))

    assert len(d._queue) == 1
    assert [call.rule_id for call in d.drain(1.0)] == ["new"]
    assert sink.spoken == ["new"]
    assert ("old", "suppressed", "superseded") in {
        (row["rule_id"], row["outcome"], row["suppressed_by"]) for row in _log(buf)
    }


def test_queued_conflict_that_still_holds_keeps_old_call() -> None:
    state = {"old_holds": True}
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: state["old_holds"]
    new = _cand("new", priority=1, conflict_group="slow_car")
    d, _, buf = _dispatcher()

    d.submit([old], _snap(0.0))
    d.submit([new], _snap(1.0))

    assert len(d._queue) == 1
    assert ("new", "suppressed", "conflict") in {
        (row["rule_id"], row["outcome"], row["suppressed_by"]) for row in _log(buf)
    }


def test_supersedes_drops_queued_call_that_still_holds() -> None:
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: True
    new = _cand("new", priority=1, supersedes=["old"])
    d, _, buf = _dispatcher()

    d.submit([old], _snap(0.0))
    d.submit([new], _snap(1.0))

    assert len(d._queue) == 1
    assert d._queue[0].call.rule_id == "new"
    assert ("old", "suppressed", "superseded") in {
        (row["rule_id"], row["outcome"], row["suppressed_by"]) for row in _log(buf)
    }


def test_stale_conflict_undoes_shared_cooldown_booking() -> None:
    old = _cand("old", priority=1, cooldown_s=30, cooldown_group="shared", conflict_group="g")
    old.current = lambda snap: False
    new = _cand("new", priority=1, cooldown_s=30, cooldown_group="shared", conflict_group="g")
    d, _, buf = _dispatcher()

    d.submit([old], _snap(0.0))
    d.submit([new], _snap(1.0))

    assert len(d._queue) == 1
    assert d._queue[0].call.rule_id == "new"
    assert not any(
        row["rule_id"] == "new" and row["suppressed_by"] == "cooldown" for row in _log(buf)
    )


def test_conflict_removes_the_matching_call_when_queue_keys_tie() -> None:
    unrelated = _cand("unrelated", priority=1, conflict_group="other")
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: False
    new = _cand("new", priority=1, conflict_group="slow_car")
    d, _, _ = _dispatcher()

    d.submit([unrelated, old], _snap(0.0))
    d.submit([new], _snap(1.0))

    assert {queued.call.rule_id for queued in d._queue} == {"unrelated", "new"}


@pytest.mark.parametrize(
    ("old_id", "new_id", "group"),
    [
        ("release_hold", "release_go", "release"),
        ("front_wing_lost_box", "front_wing_lost_nurse", "front_wing_lost"),
        ("battle_attack", "battle_patience", "battle_call"),
    ],
)
def test_real_rule_pairs_drop_stale_queued_call(old_id: str, new_id: str, group: str) -> None:
    old = _cand(old_id, priority=1, conflict_group=group)
    old.current = lambda snap: False
    new = _cand(new_id, priority=1, conflict_group=group)
    d, _, _ = _dispatcher()

    d.submit([old], _snap(0.0))
    d.submit([new], _snap(1.0))

    assert len(d._queue) == 1
    assert d._queue[0].call.rule_id == new_id


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
    d, sink, _ = _dispatcher(deadlines_s={3: 1.0}, p3_straight_wait_s=0.0)
    d.submit([_cand("p3", priority=3, text="info")], _snap(0.0))
    d.drain(0.0)
    d.drain(2.0)  # past the 1 s P3 deadline while never on a straight
    assert not sink.spoken


def test_p3_waits_for_straight_past_base_deadline() -> None:
    d, sink, _ = _dispatcher(deadlines_s={3: 1.5}, min_gap_s=0.0)
    d.submit([_cand("p3", priority=3, text="lock-up")], _snap(0.0))
    d.drain(0.0)
    d.drain(3.0)  # braking zone into the next corner: still off the straight
    assert not sink.spoken
    d.submit([], Snapshot(now=5.0, lap_num=1, on_straight=True))
    assert d.drain(5.0) and sink.spoken == ["lock-up"]
    d2, sink2, _ = _dispatcher(deadlines_s={3: 1.5}, min_gap_s=0.0)
    d2.submit([_cand("p3", priority=3, text="late")], _snap(0.0))
    d2.submit([], Snapshot(now=10.0, lap_num=1, on_straight=True))
    d2.drain(10.0)  # 1.5 s + 8 s straight wait exceeded
    assert not sink2.spoken


def test_p3_straight_only_disabled() -> None:
    d, sink, _ = _dispatcher(p3_straight_only=False)
    d.submit([_cand("p3", priority=3, text="info")], _snap(0.0))
    d.drain(0.0)
    assert sink.spoken == ["info"]


def test_ack_then_say_again() -> None:
    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="box box")], _snap(0.0))
    d.drain(0.0)
    original = next(r for r in _log(buf) if r["outcome"] == "fired")
    from pitwall.input.press import Press

    # ack with target -> logged, same-lap resubmission suppressed
    d.on_press(Press("ack", 0.5), _snap(0.5))
    d.submit([_cand("a", text="box box2")], _snap(1.0))
    assert d.drain(1.0) == []
    # ack with no live target -> say again re-speaks the last call
    d.on_press(Press("ack", 20.0), _snap(20.0))
    calls = d.drain(20.0)
    assert len(calls) == 1 and "say_again" in calls[0].tags
    replay = [r for r in _log(buf) if r["outcome"] == "fired"][-1]
    assert replay["inputs"]["repeat_of"] == original["call_id"]


def test_replay_press_grades_original_call_and_quality_counts_it_once() -> None:
    from pitwall.input.press import Press

    db = Database(":memory:")
    uid = 870
    db.upsert_session(uid)
    sink = CollectSink()
    buf = io.StringIO()
    d = Dispatcher(
        PolicySettings(min_gap_s=0.0),
        VirtualClock(),
        decision_log=DecisionLog(fp=buf, db=db, session_uid_source=lambda: uid),
        sinks=[sink],
        input=InputSettings(
            say_again=True,
            say_again_window_s=30.0,
            response_window_s=3.0,
            spoken_replies=True,
        ),
    )
    d.submit([_cand("a", text="box box")], _snap(0.0))
    original, *_ = d.drain(0.0)
    assert original is not None

    d.on_press(Press("ack", 20.0), _snap(20.0))
    d.submit([], _snap(20.0))
    (replay,) = d.drain(20.0)
    assert "say_again" in replay.tags
    assert sink.spoken == ["box box", "box box"]
    d.on_press(Press("ack", 20.5), _snap(20.5))

    grades = db.grades_for_session(uid)
    assert len(grades) == 1
    assert grades[0]["call_id"] == original.id
    assert grades[0]["source"] == "press"
    calls = db.calls_for_session(uid)
    ack = next(row for row in calls if row["outcome"] == "ack")
    assert ack["call_id"] == replay.id and ack["rule_id"] == original.rule_id
    quality = call_quality(db, uid)
    assert quality["fired"] == 1 and quality["good"] == 1


def test_say_again_ignores_stale_calls() -> None:
    from pitwall.input.press import Press

    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="pressures")], _snap(0.0))
    d.drain(0.0)
    # a press long after the call (e.g. menu buttons in the garage) repeats nothing
    d.on_press(Press("ack", 100.0), _snap(100.0))
    assert d.drain(100.0) == []


def test_late_press_bookmarks_without_say_again() -> None:
    from pitwall.input.press import Press

    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(say_again=False, spoken_replies=True)
    d.submit([_cand("a", text="pit exit clear")], _snap(0.0))
    d.drain(0.0)
    d.on_press(Press("ack", 20.0), _snap(20.0))  # past the response window
    assert [call.text for call in d.drain(20.0)] == ["Marked."]
    assert d.quiet_until is None
    bookmark = next(r for r in _log(buf) if r["outcome"] == "bookmark")
    assert bookmark["kind"] == "tap"


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

    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(spoken_replies=True)
    d.submit([_cand("a", text="box box")], _snap(0.0))
    d.drain(0.0)
    d.on_press(Press("ack", 1.0), _snap(1.0))
    calls = d.drain(1.0)
    assert len(calls) == 1 and calls[0].rule_id == "reply" and calls[0].text == "Copy."
    ack = next(r for r in _log(buf) if r["outcome"] == "ack")
    assert ack["grade"] == "good" and ack["grade_source"] == "press"
    d.on_press(Press("neg", 2.0), _snap(2.0))
    assert [c.text for c in d.drain(2.0)] == ["Noted."]
    neg = next(r for r in _log(buf) if r["outcome"] == "neg")
    assert neg["grade"] == "noise" and neg["grade_source"] == "press"


def test_ack_without_target_bookmarks_with_snapshot_context() -> None:
    from pitwall.input.press import Press

    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(spoken_replies=True, say_again=False)
    d.on_press(Press("ack", 10.0), Snapshot(now=10.0, lap_num=4, fuel_remaining_laps=3.12345))

    bookmark = next(r for r in _log(buf) if r["outcome"] == "bookmark")
    assert bookmark["kind"] == "tap"
    assert bookmark["context"]["lap_num"] == 4
    assert bookmark["context"]["fuel_remaining_laps"] == 3.123
    assert d.drain(10.0)[0].text == "Marked."


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


class AudioSink(CollectSink):
    speaks_audio = True


def test_radio_silent_keeps_screen_mutes_headset() -> None:
    from pitwall.input.press import Press

    d, screen, buf = _dispatcher(min_gap_s=0.0)
    audio = AudioSink()
    d.sinks.append(audio)
    d.input = InputSettings(spoken_replies=True, long_press="silent")
    d.on_press(Press("bookmark", 0.0), _snap(0.0))  # long press toggles
    assert d.silent is True
    d.drain(0.0)
    assert audio.spoken == ["Radio silent. Leave you to it."]
    d.submit([_cand("info", priority=2, text="gap 1.2")], _snap(1.0))
    d.submit([_cand("urgent", priority=1, text="car behind")], _snap(1.0))
    d.drain(1.0)
    d.drain(4.0)
    assert "gap 1.2" in screen.spoken and "gap 1.2" not in audio.spoken
    assert "car behind" in audio.spoken  # P1 still speaks
    d.on_press(Press("silent", 5.0), _snap(5.0))  # UDP 3 toggle
    d.drain(5.0)
    assert d.silent is False and audio.spoken[-1] == "Back with you. Feeding you info again."
    outcomes = [r["outcome"] for r in _log(buf)]
    assert "silent_on" in outcomes and "silent_off" in outcomes and "bookmark" not in outcomes


def test_long_press_still_bookmarks_by_default() -> None:
    from pitwall.input.press import Press

    d, _, buf = _dispatcher()
    d.on_press(Press("bookmark", 0.0), _snap(0.0))
    bookmark = _log(buf)[-1]
    assert d.silent is False and bookmark["outcome"] == "bookmark"
    assert bookmark["kind"] == "hold" and bookmark["context"]["lap_num"] == 1


def test_min_gap_defers_instead_of_dropping() -> None:
    d, sink, buf = _dispatcher(min_gap_s=3.0)
    d.submit([_cand("a")], _snap(0.0))
    d.drain(0.0)
    d.submit([_cand("wing", text="wing gone")], _snap(1.0))
    assert d.drain(1.0) == []  # held: radio still busy
    assert [c.rule_id for c in d.drain(3.0)] == ["wing"]
    assert not any(r.get("suppressed_by") == "budget" for r in _log(buf))


def test_min_gap_deferred_calls_are_spaced() -> None:
    d, sink, _ = _dispatcher(min_gap_s=3.0)
    d.submit([_cand("a")], _snap(0.0))
    d.drain(0.0)
    d.submit([_cand("b"), _cand("c")], _snap(0.5))
    assert [c.rule_id for c in d.drain(3.0)] == ["b"]
    assert d.drain(5.0) == []
    assert [c.rule_id for c in d.drain(6.0)] == ["c"]


def test_min_gap_drops_past_defer_limit() -> None:
    d, sink, buf = _dispatcher(min_gap_s=3.0, min_gap_defer_s=4.0)
    d.submit([_cand("a")], _snap(0.0))
    d.drain(0.0)
    d.submit([_cand("b"), _cand("c")], _snap(0.5))  # c would wait until 6.0
    assert [r["rule_id"] for r in _log(buf) if r.get("suppressed_by") == "budget"] == ["c"]


def test_urgency_order_and_preemption_matrix() -> None:
    d, _, _ = _dispatcher(
        min_gap_s=0.0,
        calls_per_lap=20,
        deadlines_s={1: 60.0, 2: 60.0, 3: 60.0},
    )
    d.submit(
        [
            _cand(
                name,
                priority=1 if urgency == "safety" else 2,
                urgency=urgency,
                **({"tags": ["systems"]} if urgency == "safety" else {}),
            )
            for name, urgency in (
                ("coach", "coaching"),
                ("info", "info"),
                ("tactical", "tactical"),
                ("execution", "execution"),
                ("safety", "safety"),
            )
        ],
        _snap(0.0),
    )
    d.menu_reply("Reply", "reply", _snap(0.0))
    d.drain(0.0)
    calls = {call.rule_id: call for call, _ in d._spoken_calls}
    assert [call.rule_id for call, _ in d._spoken_calls] == [
        "safety",
        "execution",
        "reply",
        "tactical",
        "info",
        "coach",
    ]
    assert d._preempts(calls["safety"], calls["execution"]) is True
    calls["execution"].promoted = True
    assert d._preempts(calls["safety"], calls["execution"]) is False
    assert d._preempts(calls["execution"], calls["tactical"]) is True
    calls["execution"].promoted = False
    assert d._preempts(calls["execution"], calls["tactical"]) is False
    assert d._preempts(calls["reply"], calls["tactical"]) is False
    assert d._preempts(calls["reply"], calls["info"]) is True


def test_preempting_call_holds_lower_urgency_calls_behind_it() -> None:
    d, _, _ = _dispatcher(
        min_gap_s=0.0,
        calls_per_lap=20,
        deadlines_s={1: 60.0, 2: 60.0, 3: 60.0},
    )
    d.submit([_cand("info", urgency="info", text="X" * 90)], _snap(0.0))
    assert [call.rule_id for call in d.drain(0.0)] == ["info"]

    d.submit(
        [
            _cand("safety", urgency="safety", tags=["systems"], text="S"),
            _cand("tactical", urgency="tactical", text="T"),
        ],
        _snap(0.2),
    )
    assert [call.rule_id for call in d.drain(0.2)] == ["safety"]
    safety_end = next(end_t for call, end_t in d._spoken_calls if call.rule_id == "safety")
    tactical = next(item.call for item in d._queue if item.call.rule_id == "tactical")
    assert tactical.ready_t >= safety_end
    assert [call.rule_id for call in d.drain(safety_end)] == ["tactical"]


def test_decision_distance_promotes_and_logs() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    snap = Snapshot(
        now=0.0,
        track_length_m=5000,
        pit_entry_m=1000,
        lap_distance=950,
        speed_kmh=36,
    )
    d.submit([_cand("box", urgency="execution", decision_point="pit_entry")], snap)
    call = d._queue[0].call
    assert call.decision_s == pytest.approx(5.0)
    assert call.promoted is True
    assert any(row["inputs"].get("decision_s") == pytest.approx(5.0) for row in _log(buf))


def test_nearer_promoted_execution_precedes_older_call() -> None:
    d, _, _ = _dispatcher(min_gap_s=0.0)
    snap = Snapshot(
        now=0.0,
        track_length_m=5000,
        pit_entry_m=10,
        lap_distance=4990,
        speed_kmh=36,
    )
    d.submit([_cand("pit", urgency="execution", decision_point="pit_entry")], snap)
    d.submit(
        [_cand("line", urgency="execution", decision_point="line")],
        Snapshot(
            now=0.1,
            track_length_m=5000,
            pit_entry_m=10,
            lap_distance=4990,
            speed_kmh=36,
        ),
    )
    assert {item.call.rule_id: item.call.decision_s for item in d._queue} == pytest.approx(
        {"pit": 2.0, "line": 1.0}
    )
    assert [call.rule_id for call in d.drain(0.1)] == ["line", "pit"]


def test_focus_and_systems_exemption() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("hazard", urgency="safety"), _cand("tip", urgency="info")], _snap(0.0))
    assert {item.call.rule_id for item in d._queue} == {"hazard"}
    assert ("tip", "focus") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("systems", urgency="safety", tags=["systems"])], _snap(0.0))
    d.submit([_cand("tip", urgency="info")], _snap(1.0))
    assert {item.call.rule_id for item in d._queue} == {"systems", "tip"}


def test_location_coherence_after_safety() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("hazard", urgency="safety", tags=["systems"])], _snap(0.0))
    d.drain(0.0)
    d.submit([_cand("corner", urgency="coaching", location_ref=True)], _snap(1.0))
    assert d.drain(1.0) == []
    assert ("corner", "coherence") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_conflict_scores_and_decision_missed() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    lower = _cand("lower", conflict_group="choice")
    higher = _cand("higher", conflict_group="choice")
    lower.outcome_score, higher.outcome_score = 0.2, 0.9
    d.submit([lower], _snap(0.0))
    d.submit([higher], _snap(1.0))
    assert [item.call.rule_id for item in d._queue] == ["higher"]
    assert ("lower", "conflict_loser") in {
        (row["rule_id"], row["suppressed_by"]) for row in _log(buf)
    }

    d, _, buf = _dispatcher(min_gap_s=0.0, decision_missed_s=2.0)
    live = _cand("live", conflict_group="choice")
    missed = _cand("missed", urgency="execution", decision_point="line", conflict_group="choice")
    live.outcome_score, missed.outcome_score = 0.1, 10.0
    d.submit([live], Snapshot(now=0.0, track_length_m=5000, lap_distance=4990, speed_kmh=100))
    d.submit([missed], Snapshot(now=1.0, track_length_m=5000, lap_distance=4990, speed_kmh=100))
    assert [item.call.rule_id for item in d._queue] == ["live"]
    assert ("missed", "decision_missed") in {
        (row["rule_id"], row["suppressed_by"]) for row in _log(buf)
    }


def test_rotation_is_seeded_and_both_rules_can_win() -> None:
    winners = set()
    for seed in range(1, 21):
        same_seed = []
        for _ in range(2):
            d, _, _ = _dispatcher(min_gap_s=0.0)
            d.reset_session(seed)
            d.submit([_cand("a", rotate_with=["b"]), _cand("b", rotate_with=["a"])], _snap(0.0))
            same_seed.append(d._queue[0].call.rule_id)
        assert same_seed[0] == same_seed[1]
        winners.add(same_seed[0])
    assert winners == {"a", "b"}


def test_first_submission_seeds_rotation_from_session_uid() -> None:
    for session_uid in range(1, 21):
        winners = []
        snapshot = Snapshot(now=0.0, session_uid=session_uid)
        for reset in (False, True):
            d, _, _ = _dispatcher(min_gap_s=0.0)
            if reset:
                d.reset_session(session_uid)
            d.submit(
                [_cand("a", rotate_with=["b"]), _cand("b", rotate_with=["a"])],
                snapshot,
            )
            winners.append(d._queue[0].call.rule_id)
        assert winners[0] == winners[1]


@pytest.mark.parametrize("resolver_first", [False, True])
def test_resolved_by_works_in_both_orders(resolver_first: bool) -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    target, resolver = _cand("target", resolved_by=["resolver"]), _cand("resolver")
    ordered = [resolver, target] if resolver_first else [target, resolver]
    for index, candidate in enumerate(ordered):
        d.submit([candidate], _snap(float(index)))
    assert not any(item.call.rule_id == "target" for item in d._queue)
    assert ("target", "resolved") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_escalation_and_flush_drop_queued_calls() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("low", urgency="info")], _snap(0.0))
    d.submit([_cand("high", urgency="safety", escalates=["low"])], _snap(1.0))
    assert {item.call.rule_id for item in d._queue} == {"high"}
    assert ("low", "superseded_escalation") in {
        (row["rule_id"], row["suppressed_by"]) for row in _log(buf)
    }
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("tip", urgency="info")], _snap(0.0))
    d.menu_reply("Reply", "reply", _snap(0.0))
    d.submit(
        [_cand("red", urgency="safety", flushes_queue=True, tags=["systems"])],
        _snap(1.0),
    )
    assert {item.call.rule_id for item in d._queue} == {"red"}
    assert ("tip", "flushed") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_flush_calls_preserve_other_flush_calls_in_the_same_tick() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit(
        [
            _cand("ordinary", urgency="info"),
            _cand("finish_podium", urgency="info", flushes_queue=True),
            _cand("finish_gained", urgency="info", flushes_queue=True),
        ],
        _snap(0.0),
    )

    assert {item.call.rule_id for item in d._queue} == {"finish_podium", "finish_gained"}
    assert ("ordinary", "flushed") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_obvious_and_provisional_suppression() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    obvious, provisional = _cand("obvious"), _cand("provisional")
    obvious.obvious = True
    provisional.provisional_silent = True
    d.submit([obvious, provisional], _snap(0.0))
    assert not d._queue
    assert {row["suppressed_by"] for row in _log(buf)} == {"obvious", "provisional"}


def test_reply_absorption_is_topic_specific_and_reply_waits_for_execution() -> None:
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.menu_reply("Tyres are fine", "menu:tyres", _snap(0.0), ["tyre_life"])
    d.submit([_cand("tyre_life", urgency="info")], _snap(1.0))
    assert not any(item.call.rule_id == "menu:tyres" for item in d._queue)
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.menu_reply("Tyres are fine", "menu:tyres", _snap(0.0), ["tyre_life"])
    d.submit([_cand("box_now", urgency="execution")], _snap(1.0))
    assert any(item.call.rule_id == "menu:tyres" for item in d._queue)
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.menu_reply("Tyres are fine", "menu:tyres", _snap(0.0), ["tyre_life"])
    d.submit([_cand("box", urgency="execution")], _snap(1.0))
    assert [item.call.urgency for item in sorted(d._queue)] == ["execution", "reply"]
    assert {item.call.rule_id for item in d._queue} == {"box", "menu:tyres"}


def test_safety_cut_requeues_reply_once() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.menu_reply(
        "A long answer that keeps speaking while safety arrives",
        "menu:answer",
        _snap(0.0),
    )
    assert [call.rule_id for call in d.drain(0.0)] == ["menu:answer"]
    d.submit([_cand("hazard", urgency="safety", tags=["systems"])], _snap(0.5))
    assert [call.rule_id for call in d.drain(0.5)] == ["hazard"]
    reply = next(item.call for item in d._queue if item.call.rule_id == "menu:answer")
    assert reply.requeued
    assert sum(call.rule_id == "menu:answer" for call, _ in d._spoken_calls) == 1
    assert ("menu:answer", "requeued", "preempted") in {
        (row["rule_id"], row["outcome"], row["suppressed_by"]) for row in _log(buf)
    }
    safety_end = next(end_t for call, end_t in d._spoken_calls if call.rule_id == "hazard")
    assert [call.rule_id for call in d.drain(safety_end)] == ["menu:answer"]


def test_coalescing_renders_count_word() -> None:
    template = ["{count_word} slow cars ahead. Careful"]
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("slow", say_many=template)], _snap(0.0))
    d.submit([_cand("slow", say_many=template)], _snap(0.1))
    assert d.drain(0.1)[0].text == "two slow cars ahead. Careful"


def test_digest_uses_freshest_briefs_caps_items_and_skips_battle() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0, digest_max_items=2)
    for name, now in (("old", 0.0), ("newer", 1.0), ("newest", 2.0)):
        candidate = _cand(name, urgency="info")
        candidate.brief = name
        d.submit([candidate], _snap(now))
    emitted = d.drain(2.0)
    assert len(emitted) == 1 and emitted[0].rule_id == "digest"
    assert "newer" in emitted[0].text and "newest" in emitted[0].text
    assert "old" not in emitted[0].text
    assert any(row["outcome"] == "digest_overflow" for row in _log(buf))
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit(
        [_cand("one", urgency="info"), _cand("two", urgency="info")],
        Snapshot(now=0.0, gap_ahead_s=0.5, gap_behind_s=0.5),
    )
    assert {call.rule_id for call in d.drain(0.0)} == {"one", "two"}
    assert not any(row["rule_id"] == "digest" for row in _log(buf))


def test_digest_waits_until_current_speaker_finishes() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0, digest_max_items=2)
    d.submit([_cand("tactical", urgency="tactical", text="T" * 90)], _snap(0.0))
    assert [call.rule_id for call in d.drain(0.0)] == ["tactical"]
    for name in ("brief one", "brief two"):
        candidate = _cand(name.replace(" ", "_"), urgency="info")
        candidate.brief = name
        d.submit([candidate], _snap(0.2))

    assert d.drain(0.3) == []
    assert not any(row["outcome"] == "digested" for row in _log(buf))
    assert {item.call.rule_id for item in d._queue} == {"brief_one", "brief_two"}

    speech_end = next(end_t for call, end_t in d._spoken_calls if call.rule_id == "tactical")
    digest_calls = d.drain(speech_end)
    assert len(digest_calls) == 1
    assert digest_calls[0].rule_id == "digest"
    assert "brief one" in digest_calls[0].text and "brief two" in digest_calls[0].text


def test_every_priority_one_rule_has_explicit_urgency() -> None:
    settings = ConfigStore(isolated=True).current()
    missing = [
        rule.id
        for rule in settings.rules
        if (rule.priority == 1 or any(item.priority == 1 for item in rule.severity))
        and rule.urgency is None
    ]
    assert not missing


def test_drs_fault_urgency_tracks_priority() -> None:
    settings = ConfigStore(isolated=True).current()
    drs_fault = next(rule for rule in settings.rules if rule.id == "drs_fault")
    assert drs_fault.urgency_class(1) == "safety"
    assert drs_fault.urgency_class(2) == "tactical"
