from __future__ import annotations

import io
import json
import logging
from functools import partial

import pytest

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import InputSettings, PolicySettings, RuleDefModel
from pitwall.digest import call_quality
from pitwall.metrics import Metrics
from pitwall.rules.engine import Candidate, Rule, RuleEngine
from pitwall.state.session import Snapshot
from pitwall.store.db import Database

from .factories import make_snapshot as _make_snapshot

make_snapshot = partial(_make_snapshot, lap_num=1)


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


def _log(buf: io.StringIO) -> list[dict]:
    buf.seek(0)
    return [json.loads(line) for line in buf if line.strip()]


def _battle_engine() -> RuleEngine:
    store = ConfigStore()
    settings = store.current()
    return RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=store.current().resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )


def _battle_snapshot(now: float = 0.0, **overrides: object) -> Snapshot:
    values: dict[str, object] = {
        "session_kind": "race",
        "session_type": 15,
        "phase": "racing",
        "lap_num": 5,
        "laps_remaining": 10,
        "battle_mode": "defending",
        "battle_hold_prob": 0.15,
        "battle_tyre_known_behind": True,
        "battle_tyre_offset_behind": -6,
        "battle_threat_laps": 0.0,
        "rival_behind_name": "NORRIS",
        "rival_behind_idx": 4,
        "gap_behind_s": 0.6,
        "battle_closing_behind_s": 0.4,
        "sector": 0,
        "_ages": {"car_telemetry": 0.1, "lap_data": 0.1},
    }
    values.update(overrides)
    return make_snapshot(now, **values)


def _battle_tick(
    engine: RuleEngine, dispatcher: Dispatcher, now: float, **overrides: object
) -> tuple[Snapshot, list[Candidate], list[Call]]:
    snapshot = _battle_snapshot(now, **overrides)
    result = engine.evaluate(snapshot)
    candidates = [
        candidate
        for candidate in result.candidates
        if candidate.rule.id in {"battle_defend", "battle_let_by"}
    ]
    dispatcher.submit(candidates, snapshot)
    return snapshot, candidates, dispatcher.drain(now)


def test_quiet_and_verbosity() -> None:
    d, sink, _ = _dispatcher(quiet=True)
    d.submit([_cand("a")], make_snapshot(0.0))
    assert d.drain(0.0) == []
    assert not sink.spoken

    d, sink, _ = _dispatcher(verbosity="critical")
    d.submit([_cand("p2", priority=2), _cand("p1", priority=1)], make_snapshot(0.0))
    calls = d.drain(0.0)
    assert [c.rule_id for c in calls] == ["p1"]


def test_cooldown_and_dedupe() -> None:
    d, sink, _ = _dispatcher()
    d.submit([_cand("a", cooldown_s=30, text="hi")], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([_cand("a", cooldown_s=30, text="hi")], make_snapshot(10.0))
    assert d.drain(10.0) == []  # cooldown
    d.submit([_cand("b", text="hi")], make_snapshot(10.0))
    assert d.drain(10.0) == []  # same text inside dedupe window
    fresh = _cand("a", cooldown_s=30, text="hi")
    fresh.trigger_t = 31.0
    d.submit([fresh], make_snapshot(31.0))
    assert len(d.drain(31.0)) == 1


def test_budget_and_p1_exempt() -> None:
    d, sink, _ = _dispatcher(calls_per_lap=1, min_gap_s=0.0)
    d.submit([_cand("a"), _cand("b")], make_snapshot(0.0))
    d.submit([_cand("c", priority=1)], make_snapshot(0.0))
    calls = d.drain(0.0)
    assert [c.rule_id for c in calls] == ["c", "a"]  # P1 preempts the budget


def test_min_gap() -> None:
    d, sink, _ = _dispatcher(min_gap_s=5.0)
    d.submit([_cand("a")], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([_cand("b")], make_snapshot(1.0))
    assert d.drain(1.0) == []
    d.submit([_cand("c")], make_snapshot(6.0))
    assert len(d.drain(6.0)) == 1


def test_still_true_error_warns_once_and_keeps_call(caplog: pytest.LogCaptureFixture) -> None:
    d, sink, _ = _dispatcher()
    candidate = _cand("still_true_broken")

    def broken(_snapshot: Snapshot) -> bool:
        raise RuntimeError("predicate failed")

    candidate.still_true = broken
    with caplog.at_level(logging.WARNING, logger="pitwall.audio.dispatcher"):
        d.submit([candidate], make_snapshot(0.0))
        calls = d.drain(0.0)
        assert len(calls) == 1
        assert sink.spoken == ["still_true_broken"]
        assert d._digest_call_ready(calls[0], 0.0, on_straight=True)
        assert d._digest_call_ready(calls[0], 0.0, on_straight=True)

    warnings = [record.getMessage() for record in caplog.records]
    assert sum("drain.still_true" in message for message in warnings) == 1
    assert sum("digest.still_true" in message for message in warnings) == 1


def test_deadline_drop() -> None:
    d, sink, _ = _dispatcher(deadlines_s={1: 5.0, 2: 0.5, 3: 1.5})
    d.submit([_cand("a")], make_snapshot(0.0))
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


@pytest.mark.parametrize(("drain_t", "expected"), [(10.4, "fired"), (10.6, "stale")])
def test_p3_waiting_on_min_gap_expires_from_its_trigger(drain_t: float, expected: str) -> None:
    d, sink, buf = _dispatcher(min_gap_s=3.0)
    d.submit([_cand("p2", text="p2")], Snapshot(now=0.0))
    d.drain(0.0)

    p3 = _cand("p3", priority=3, text="info")
    p3.trigger_t = 0.5
    d.submit([p3], Snapshot(now=0.5))
    assert d.drain(0.5) == []
    assert d._queue[0].call.not_before > 0.5
    d.submit([], Snapshot(now=drain_t, on_straight=True))

    emitted = d.drain(drain_t)
    rows = [row for row in _log(buf) if row["rule_id"] == "p3"]
    if expected == "fired":
        assert [call.rule_id for call in emitted] == ["p3"]
        assert any(row["outcome"] == "fired" for row in rows)
        assert sink.spoken == ["p2", "info"]
    else:
        assert not emitted
        assert any(
            row["outcome"] == "suppressed" and row["suppressed_by"] == "stale" for row in rows
        )
        assert not any(row["outcome"] == "fired" for row in rows)
        assert sink.spoken == ["p2"]


def test_stale_call_held_behind_long_speech_is_suppressed() -> None:
    d, sink, buf = _dispatcher(min_gap_s=0.0)
    long_text = "p" * 200
    d.submit([_cand("long", text=long_text)], Snapshot(now=0.0))
    d.drain(0.0)

    held = _cand("held")
    held.trigger_t = 0.5
    d.submit([held], Snapshot(now=0.5))
    assert d.drain(0.5) == []
    assert d._queue[0].call.ready_t > 10.5
    d.submit([], Snapshot(now=13.5))

    assert d.drain(13.5) == []
    rows = [row for row in _log(buf) if row["rule_id"] == "held"]
    assert any(row["outcome"] == "suppressed" and row["suppressed_by"] == "stale" for row in rows)
    assert not any(row["outcome"] == "fired" for row in rows)
    assert sink.spoken == [long_text]


def test_reply_tag_exempts_old_queued_call_from_age_cap() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    reply = _cand("reply", tags=["reply"])
    d.submit([reply], Snapshot(now=0.0))
    queued = d._queue[0].call
    queued.not_before = 11.0
    d.submit([], Snapshot(now=11.0))

    emitted = d.drain(11.0)

    assert emitted == [queued]
    assert sink.spoken == ["reply"]
    assert queued.max_age_s is None


def test_dropped_call_is_removed_and_logged_on_drain() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("dropped")], Snapshot(now=0.0))
    call = d.drain(0.0)[0]

    d.note_dropped(call.id)
    assert d.drain(1.0) == []

    assert d._current is None
    assert not any(spoken.id == call.id for spoken, _ in d._spoken_calls)
    assert any(
        row["rule_id"] == "dropped"
        and row["outcome"] == "suppressed"
        and row["suppressed_by"] == "stale"
        for row in _log(buf)
    )


def test_queue_status_and_call_handoff_fields() -> None:
    d, _, _ = _dispatcher(min_gap_s=0.0)
    first = _cand("first")
    second = _cand("second")
    second.trigger_t = 0.5
    d.submit([first, second], Snapshot(now=0.0))
    d.menu_prompt("menu item", Snapshot(now=0.5))

    assert d.queue_status(5.0) == (2, 5.0)
    d.submit([], Snapshot(now=6.0))
    assert d.metrics.summary()["voice_queue"] == {"count": 2, "oldest_s": 6.0}

    fired, _, _ = _dispatcher(min_gap_s=0.0)
    fired.submit([_cand("normal")], Snapshot(now=0.0))
    call = fired.drain(2.0)[0]
    assert call.handoff_age_s == 2.0
    assert call.max_age_s == 10.0


def test_metrics_track_queue_wait_and_one_tts_sample_per_call() -> None:
    metrics = Metrics()
    metrics.note_queue_wait(10.0, 10.25)
    metrics.note_handoff("call", 100.0)
    metrics.note_spoken("call", 100.05)
    metrics.note_spoken("call", 100.10)

    summary = metrics.summary()

    assert summary["queue_wait_ms"]["p99"] == pytest.approx(250.0)
    assert summary["tts_ms"]["mean"] == pytest.approx(50.0)
    assert summary["voice_queue"] == {"count": 0, "oldest_s": 0.0}
    assert "trigger_to_speak_ms" not in summary


def test_revalidation() -> None:
    cand = _cand("a")
    cand.still_true = lambda snap: snap.lap_num > 0  # type: ignore[method-assign]
    d, sink, _ = _dispatcher()
    d.submit([cand], make_snapshot(0.0, lap_num=1))
    assert len(d.drain(0.0)) == 1

    cand2 = _cand("b")
    cand2.still_true = lambda snap: False  # type: ignore[method-assign]
    d.submit([cand2], make_snapshot(1.0, lap_num=1))
    assert d.drain(1.0) == []


def test_battle_let_by_rearms_after_shared_battle_cooldown() -> None:
    engine = _battle_engine()
    dispatcher, sink, buf = _dispatcher(min_gap_s=0.0)

    _, candidates, emitted = _battle_tick(engine, dispatcher, 0.0, battle_hold_prob=0.5)
    assert "battle_defend" in {candidate.rule.id for candidate in candidates}
    assert any(call.rule_id == "battle_defend" for call in emitted)

    _, candidates, emitted = _battle_tick(engine, dispatcher, 1.0)
    assert "battle_let_by" in {candidate.rule.id for candidate in candidates}
    assert not any(call.rule_id == "battle_let_by" for call in emitted)
    assert any(
        row["rule_id"] == "battle_let_by"
        and row["outcome"] == "suppressed"
        and row["suppressed_by"] == "cooldown"
        for row in _log(buf)
    )

    _, candidates, _ = _battle_tick(engine, dispatcher, 121.0, sector=2)
    assert "battle_let_by" not in {candidate.rule.id for candidate in candidates}
    _, candidates, emitted = _battle_tick(engine, dispatcher, 122.0, sector=0)
    assert "battle_let_by" in {candidate.rule.id for candidate in candidates}
    assert any(call.rule_id == "battle_let_by" for call in emitted)
    let_by_call = next(call for call in emitted if call.rule_id == "battle_let_by")
    assert let_by_call.text in sink.spoken


def test_battle_let_by_rearms_when_eligibility_clears() -> None:
    engine = _battle_engine()
    dispatcher, _, _ = _dispatcher(min_gap_s=0.0)

    _, candidates, emitted = _battle_tick(engine, dispatcher, 0.0)
    assert "battle_let_by" in {candidate.rule.id for candidate in candidates}
    assert any(call.rule_id == "battle_let_by" for call in emitted)

    _, candidates, _ = _battle_tick(engine, dispatcher, 1.0, battle_hold_prob=0.5)
    assert "battle_let_by" not in {candidate.rule.id for candidate in candidates}

    _, candidates, emitted = _battle_tick(engine, dispatcher, 121.0)
    assert "battle_let_by" in {candidate.rule.id for candidate in candidates}
    assert any(call.rule_id == "battle_let_by" for call in emitted)


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"rival_behind_idx": 5}, "rebound"),
        ({"battle_hold_prob": 0.5}, "revalidation"),
    ],
)
def test_queued_battle_let_by_is_dropped_for_changed_state(
    changed: dict[str, object], reason: str
) -> None:
    dispatcher, _, buf = _dispatcher(min_gap_s=5.0)
    dispatcher.submit([_cand("blocker")], make_snapshot(0.0))
    dispatcher.drain(0.0)

    engine = _battle_engine()
    snapshot = _battle_snapshot(4.5)
    result = engine.evaluate(snapshot)
    candidate = next(item for item in result.candidates if item.rule.id == "battle_let_by")
    assert candidate.bound == {"rival_behind_idx": 4}
    dispatcher.submit([candidate], snapshot)
    call = dispatcher._queue[0].call
    assert call.bound == {"rival_behind_idx": 4}

    latest = _battle_snapshot(4.6, **changed)
    dispatcher.submit([], latest)
    if reason == "rebound":
        call.not_before = 0.0
        assert not dispatcher._digest_call_ready(call, 4.6, on_straight=True)
    assert dispatcher.drain(5.0) == []
    assert any(
        row["rule_id"] == "battle_let_by"
        and row["outcome"] == "suppressed"
        and row["suppressed_by"] == reason
        for row in _log(buf)
    )


def test_rule_bind_defaults_to_empty_and_keeps_configured_fields() -> None:
    base = {"id": "bind_test", "priority": 2, "when": "True", "say": "Call"}
    assert RuleDefModel.model_validate(base).bind == []
    assert RuleDefModel.model_validate({**base, "bind": ["rival_behind_idx"]}).bind == [
        "rival_behind_idx"
    ]


def test_stale_queued_call_in_group_is_superseded() -> None:
    state = {"old_holds": True}
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: state["old_holds"]
    new = _cand("new", priority=1, conflict_group="slow_car")
    new.current = lambda snap: True
    d, sink, buf = _dispatcher()

    d.submit([old], make_snapshot(0.0))
    state["old_holds"] = False
    d.submit([new], make_snapshot(1.0))

    assert len(d._queue) == 1
    assert [call.rule_id for call in d.drain(1.0)] == ["new"]
    assert sink.spoken == ["new"]
    assert ("old", "suppressed", "superseded") in {
        (row["rule_id"], row["outcome"], row["suppressed_by"]) for row in _log(buf)
    }


def test_queued_conflict_that_still_holds_yields_to_the_newest_call() -> None:
    state = {"old_holds": True}
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: state["old_holds"]
    new = _cand("new", priority=1, conflict_group="slow_car")
    d, _, buf = _dispatcher()

    d.submit([old], make_snapshot(0.0))
    d.submit([new], make_snapshot(1.0))

    assert [item.call.rule_id for item in d._queue] == ["new"]
    assert ("old", "suppressed", "conflict") in {
        (row["rule_id"], row["outcome"], row["suppressed_by"]) for row in _log(buf)
    }


def test_supersedes_drops_queued_call_that_still_holds() -> None:
    old = _cand("old", priority=1, conflict_group="slow_car")
    old.current = lambda snap: True
    new = _cand("new", priority=1, supersedes=["old"])
    d, _, buf = _dispatcher()

    d.submit([old], make_snapshot(0.0))
    d.submit([new], make_snapshot(1.0))

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

    d.submit([old], make_snapshot(0.0))
    d.submit([new], make_snapshot(1.0))

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

    d.submit([unrelated, old], make_snapshot(0.0))
    d.submit([new], make_snapshot(1.0))

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

    d.submit([old], make_snapshot(0.0))
    d.submit([new], make_snapshot(1.0))

    assert len(d._queue) == 1
    assert d._queue[0].call.rule_id == new_id


def test_log_records_outcomes() -> None:
    d, sink, buf = _dispatcher()
    d.submit([_cand("a"), _cand("b", priority=1)], make_snapshot(0.0))
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
    d.submit(cands, make_snapshot(0.0))
    calls = d.drain(0.0)
    assert len(calls) == 4  # normal preset budget

    d, sink, _ = _dispatcher(verbosity="coach", min_gap_s=0.0)
    d.submit([_cand(f"r{i}") for i in range(10)], make_snapshot(0.0))
    assert len(d.drain(0.0)) == 8

    # explicit calls_per_lap overrides the preset
    d, sink, _ = _dispatcher(verbosity="coach", calls_per_lap=2, min_gap_s=0.0)
    d.submit([_cand(f"r{i}") for i in range(6)], make_snapshot(0.0))
    assert len(d.drain(0.0)) == 2


def test_p3_held_until_on_straight() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("p3", priority=3, text="info")], make_snapshot(0.0))
    d.drain(0.0)  # snapshot default on_straight=False
    assert not sink.spoken
    # flip on_straight via a new snapshot on the next submit tick
    d.submit([], Snapshot(now=1.0, lap_num=1, on_straight=True))
    assert d.drain(1.0) and sink.spoken == ["info"]


def test_p3_dropped_at_deadline_off_straight() -> None:
    d, sink, _ = _dispatcher(deadlines_s={3: 1.0}, p3_straight_wait_s=0.0)
    d.submit([_cand("p3", priority=3, text="info")], make_snapshot(0.0))
    d.drain(0.0)
    d.drain(2.0)  # past the 1 s P3 deadline while never on a straight
    assert not sink.spoken


def test_p3_waits_for_straight_past_base_deadline() -> None:
    d, sink, _ = _dispatcher(deadlines_s={3: 1.5}, min_gap_s=0.0)
    d.submit([_cand("p3", priority=3, text="lock-up")], make_snapshot(0.0))
    d.drain(0.0)
    d.drain(3.0)  # braking zone into the next corner: still off the straight
    assert not sink.spoken
    d.submit([], Snapshot(now=5.0, lap_num=1, on_straight=True))
    assert d.drain(5.0) and sink.spoken == ["lock-up"]
    d2, sink2, _ = _dispatcher(deadlines_s={3: 1.5}, min_gap_s=0.0)
    d2.submit([_cand("p3", priority=3, text="late")], make_snapshot(0.0))
    d2.submit([], Snapshot(now=10.0, lap_num=1, on_straight=True))
    d2.drain(10.0)  # 1.5 s + 8 s straight wait exceeded
    assert not sink2.spoken


def test_p3_straight_only_disabled() -> None:
    d, sink, _ = _dispatcher(p3_straight_only=False)
    d.submit([_cand("p3", priority=3, text="info")], make_snapshot(0.0))
    d.drain(0.0)
    assert sink.spoken == ["info"]


def test_ack_then_say_again() -> None:
    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="box box")], make_snapshot(0.0))
    d.drain(0.0)
    original = next(r for r in _log(buf) if r["outcome"] == "fired")
    from pitwall.input.press import Press

    # ack with target -> logged, same-lap resubmission suppressed
    d.on_press(Press("ack", 0.5), make_snapshot(0.5))
    d.submit([_cand("a", text="box box2")], make_snapshot(1.0))
    assert d.drain(1.0) == []
    # ack with no live target -> say again re-speaks the last call
    d.on_press(Press("ack", 20.0), make_snapshot(20.0))
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
    d.submit([_cand("a", text="box box")], make_snapshot(0.0))
    original, *_ = d.drain(0.0)
    assert original is not None

    d.on_press(Press("ack", 20.0), make_snapshot(20.0))
    d.submit([], make_snapshot(20.0))
    (replay,) = d.drain(20.0)
    assert "say_again" in replay.tags
    assert sink.spoken == ["box box", "box box"]
    d.on_press(Press("ack", 20.5), make_snapshot(20.5))

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
    d.submit([_cand("a", text="pressures")], make_snapshot(0.0))
    d.drain(0.0)
    # a press long after the call (e.g. menu buttons in the garage) repeats nothing
    d.on_press(Press("ack", 100.0), make_snapshot(100.0))
    assert d.drain(100.0) == []


def test_late_press_bookmarks_without_say_again() -> None:
    from pitwall.input.press import Press

    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(say_again=False, spoken_replies=True)
    d.submit([_cand("a", text="pit exit clear")], make_snapshot(0.0))
    d.drain(0.0)
    d.on_press(Press("ack", 20.0), make_snapshot(20.0))  # past the response window
    assert [call.text for call in d.drain(20.0)] == ["Marked."]
    assert d.quiet_until is None
    bookmark = next(r for r in _log(buf) if r["outcome"] == "bookmark")
    assert bookmark["kind"] == "tap"


def test_negative_backoff_mutes_not_p1() -> None:
    d, sink, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("a", text="call")], make_snapshot(0.0, lap_num=1))
    d.drain(0.0)
    from pitwall.input.press import Press

    d.on_press(Press("neg", 1.0), make_snapshot(1.0, lap_num=1))
    # muted for 3 laps
    for lap in (2, 3):
        now = 10.0 + lap
        candidate = _cand("a", text="again")
        candidate.trigger_t = now
        d.submit([candidate], make_snapshot(now, lap_num=lap))
        assert d.drain(10.0 + lap) == []
    fresh = _cand("a", text="again")
    fresh.trigger_t = 20.0
    d.submit([fresh], make_snapshot(20.0, lap_num=4))
    assert len(d.drain(20.0)) == 1
    # P1 is never muted
    d2, _, _ = _dispatcher(min_gap_s=0.0)
    d2.submit([_cand("crit", priority=1, text="danger")], make_snapshot(0.0, lap_num=1))
    d2.drain(0.0)
    d2.on_press(Press("neg", 1.0), make_snapshot(1.0, lap_num=1))
    d2.submit([_cand("crit", priority=1, text="danger2")], make_snapshot(2.0, lap_num=2))
    assert len(d2.drain(2.0)) == 1


def test_neg_no_target_sets_quiet_until_p1_speaks() -> None:
    d, sink, _ = _dispatcher(min_gap_s=0.0)
    from pitwall.input.press import Press

    d.on_press(Press("neg", 10.0), make_snapshot(10.0))
    assert d.quiet_until is not None
    info = _cand("a", text="info")
    info.trigger_t = 11.0
    d.submit([info], make_snapshot(11.0))
    assert d.drain(11.0) == []
    danger = _cand("crit", priority=1, text="danger")
    danger.trigger_t = 11.0
    d.submit([danger], make_snapshot(11.0))
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
    d.submit([_cand("a", text="screen only", screen_only=True)], make_snapshot(0.0))
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
    d.submit([_cand("a", text="box box")], make_snapshot(0.0))
    d.drain(0.0)
    d.on_press(Press("ack", 1.0), make_snapshot(1.0))
    calls = d.drain(1.0)
    assert len(calls) == 1 and calls[0].rule_id == "reply" and calls[0].text == "Copy."
    ack = next(r for r in _log(buf) if r["outcome"] == "ack")
    assert ack["grade"] == "good" and ack["grade_source"] == "press"
    d.on_press(Press("neg", 2.0), make_snapshot(2.0))
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
    d.on_press(Press("neg", 0.0), make_snapshot(0.0))
    assert d.quiet_until is not None
    assert "going quiet" in d.drain(0.0)[0].text
    d.on_press(Press("ack", 10.0), make_snapshot(10.0))
    assert d.quiet_until is None
    assert d.drain(10.0)[0].text == "Radio's back on."


def test_rule_reply_and_longer_response_window() -> None:
    from pitwall.input.press import Press

    d, sink, _ = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(spoken_replies=True)
    cand = _cand(
        "through", text="no need to push", response_window_s=30, on_neg=["Your call, push on"]
    )
    d.submit([cand], make_snapshot(0.0))
    d.drain(0.0)
    d.on_press(Press("neg", 16.0), make_snapshot(16.0))  # past the default 8 s window
    assert d.quiet_until is None
    assert [c.text for c in d.drain(16.0)] == ["Your call, push on"]


def test_no_response_uses_configured_window_and_logs_assumed_answer() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    responses: list[tuple[str, str]] = []
    d.on_response = lambda rule_id, answer, _: responses.append((rule_id, answer))
    d.submit(
        [
            _cand(
                "cool_push_check",
                priority=1,
                text="Confirm?",
                response_window_s=5.0,
                no_response="ack",
            )
        ],
        make_snapshot(0.0),
    )
    assert len(d.drain(0.0)) == 1
    end_t = d._spoken_calls[-1][1]

    d.submit([], make_snapshot(end_t + 5.1))
    d.drain()

    assert responses == [("cool_push_check", "ack")]
    record = next(row for row in _log(buf) if row["outcome"] == "no_response")
    assert record["rule_id"] == "cool_push_check"
    assert record["assumed_answer"] == "ack"
    assert record["suppressed_by"] == "ack"


def test_no_response_uses_input_window_when_rule_does_not_override_it() -> None:
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.input = InputSettings(response_window_s=2.0)
    responses: list[tuple[str, str]] = []
    d.on_response = lambda rule_id, answer, _: responses.append((rule_id, answer))
    d.submit(
        [_cand("prompt", priority=1, text="Confirm?", no_response="neg")],
        make_snapshot(0.0),
    )
    d.drain(0.0)
    end_t = d._spoken_calls[-1][1]

    d.submit([], make_snapshot(end_t + 1.9))
    d.drain()
    assert not responses
    d.submit([], make_snapshot(end_t + 2.1))
    d.drain()
    assert responses == [("prompt", "neg")]


@pytest.mark.parametrize(("kind", "answer"), [("ack", "ack"), ("neg", "neg")])
def test_press_answers_no_response_prompt_without_later_timeout(kind: str, answer: str) -> None:
    from pitwall.input.press import Press

    d, _, buf = _dispatcher(min_gap_s=0.0)
    responses: list[tuple[str, str]] = []
    d.on_response = lambda rule_id, result, _: responses.append((rule_id, result))
    d.submit(
        [
            _cand(
                "cool_push_check",
                priority=1,
                text="Confirm?",
                response_window_s=5.0,
                no_response="ack",
            )
        ],
        make_snapshot(0.0),
    )
    d.drain(0.0)
    end_t = d._spoken_calls[-1][1]
    press_t = end_t + 1.0

    d.on_press(Press(kind, press_t), make_snapshot(press_t))
    d.submit([], make_snapshot(end_t + 5.1))
    d.drain()

    assert responses == [("cool_push_check", answer)]
    assert not any(row["outcome"] == "no_response" for row in _log(buf))


def test_reset_session_clears_awaiting_no_response_calls() -> None:
    d, _, _ = _dispatcher(min_gap_s=0.0)
    responses: list[tuple[str, str]] = []
    d.on_response = lambda rule_id, answer, _: responses.append((rule_id, answer))
    d.submit(
        [_cand("prompt", priority=1, text="Confirm?", response_window_s=1, no_response="ack")],
        make_snapshot(0.0),
    )
    d.drain(0.0)
    assert d._awaiting_responses

    d.reset_session(2)
    d.submit([], make_snapshot(100.0))
    d.drain()

    assert not d._awaiting_responses
    assert not responses


def test_deadline_drop_assumes_no_response() -> None:
    d, _, buf = _dispatcher(deadlines_s={1: 0.5}, min_gap_s=0.0)
    responses: list[tuple[str, str]] = []
    d.on_response = lambda rule_id, answer, _: responses.append((rule_id, answer))
    d.submit(
        [
            _cand(
                "cool_push_check",
                priority=1,
                text="Confirm?",
                response_window_s=5.0,
                no_response="ack",
            )
        ],
        make_snapshot(0.0),
    )

    assert d.drain(1.0) == []
    assert responses == [("cool_push_check", "ack")]
    assert any(row["outcome"] == "no_response" for row in _log(buf))


def test_rule_without_no_response_does_not_timeout() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    responses: list[tuple[str, str]] = []
    d.on_response = lambda rule_id, answer, _: responses.append((rule_id, answer))
    d.submit([_cand("ordinary", priority=1, text="Hello")], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([], make_snapshot(100.0))
    d.drain()

    assert not responses
    assert not any(row["outcome"] == "no_response" for row in _log(buf))


class AudioSink(CollectSink):
    speaks_audio = True


def test_radio_silent_keeps_screen_mutes_headset() -> None:
    from pitwall.input.press import Press

    d, screen, buf = _dispatcher(min_gap_s=0.0)
    audio = AudioSink()
    d.sinks.append(audio)
    d.input = InputSettings(spoken_replies=True, long_press="silent")
    d.on_press(Press("bookmark", 0.0), make_snapshot(0.0))  # long press toggles
    assert d.silent is True
    d.drain(0.0)
    assert audio.spoken == ["Radio silent. Leave you to it."]
    d.submit([_cand("info", priority=2, text="gap 1.2")], make_snapshot(1.0))
    d.submit([_cand("urgent", priority=1, text="car behind")], make_snapshot(1.0))
    d.drain(1.0)
    d.drain(4.0)
    assert "gap 1.2" in screen.spoken and "gap 1.2" not in audio.spoken
    assert "car behind" in audio.spoken  # P1 still speaks
    d.on_press(Press("silent", 5.0), make_snapshot(5.0))  # UDP 3 toggle
    d.drain(5.0)
    assert d.silent is False and audio.spoken[-1] == "Back with you. Feeding you info again."
    outcomes = [r["outcome"] for r in _log(buf)]
    assert "silent_on" in outcomes and "silent_off" in outcomes and "bookmark" not in outcomes


def test_long_press_still_bookmarks_by_default() -> None:
    from pitwall.input.press import Press

    d, _, buf = _dispatcher()
    d.on_press(Press("bookmark", 0.0), make_snapshot(0.0))
    bookmark = _log(buf)[-1]
    assert d.silent is False and bookmark["outcome"] == "bookmark"
    assert bookmark["kind"] == "hold" and bookmark["context"]["lap_num"] == 1


def test_min_gap_defers_instead_of_dropping() -> None:
    d, sink, buf = _dispatcher(min_gap_s=3.0)
    d.submit([_cand("a")], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([_cand("wing", text="wing gone")], make_snapshot(1.0))
    assert d.drain(1.0) == []  # held: radio still busy
    assert [c.rule_id for c in d.drain(3.0)] == ["wing"]
    assert not any(r.get("suppressed_by") == "budget" for r in _log(buf))


def test_min_gap_deferred_calls_are_spaced() -> None:
    d, sink, _ = _dispatcher(min_gap_s=3.0)
    d.submit([_cand("a")], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([_cand("b"), _cand("c")], make_snapshot(0.5))
    assert [c.rule_id for c in d.drain(3.0)] == ["b"]
    assert d.drain(5.0) == []
    assert [c.rule_id for c in d.drain(6.0)] == ["c"]


def test_min_gap_drops_past_defer_limit() -> None:
    d, sink, buf = _dispatcher(min_gap_s=3.0, min_gap_defer_s=4.0)
    d.submit([_cand("a")], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([_cand("b"), _cand("c")], make_snapshot(0.5))  # c would wait until 6.0
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
        make_snapshot(0.0),
    )
    d.menu_reply("Reply", "reply", make_snapshot(0.0))
    d.drain(0.0)
    calls = {call.rule_id: call for call, _ in d._spoken_calls}
    # The tactical call starts a focus window that drops the coaching tip.
    assert [call.rule_id for call, _ in d._spoken_calls] == [
        "safety",
        "execution",
        "reply",
        "tactical",
        "info",
    ]
    assert d._preempts(calls["safety"], calls["execution"]) is True
    calls["execution"].promoted = True
    assert d._preempts(calls["safety"], calls["execution"]) is True
    assert d._preempts(calls["execution"], calls["tactical"]) is True
    calls["execution"].promoted = False
    assert d._preempts(calls["execution"], calls["tactical"]) is False
    assert d._preempts(calls["execution"], calls["info"]) is False
    calls["execution"].priority = 1
    assert d._preempts(calls["execution"], calls["info"]) is True
    calls["execution"].promoted = True
    assert d._preempts(calls["execution"], calls["reply"]) is True
    assert d._preempts(calls["reply"], calls["tactical"]) is False
    assert d._preempts(calls["reply"], calls["info"]) is False


def test_preempting_call_holds_lower_urgency_calls_behind_it() -> None:
    d, _, _ = _dispatcher(
        min_gap_s=0.0,
        calls_per_lap=20,
        deadlines_s={1: 60.0, 2: 60.0, 3: 60.0},
    )
    d.submit([_cand("info", urgency="info", text="X" * 90)], make_snapshot(0.0))
    assert [call.rule_id for call in d.drain(0.0)] == ["info"]

    d.submit(
        [
            _cand("safety", urgency="safety", tags=["systems"], text="S"),
            _cand("tactical", urgency="tactical", text="T"),
        ],
        make_snapshot(0.2),
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
    d.submit(
        [
            _cand("hazard", urgency="safety"),
            _cand("tip", priority=3, urgency="info"),
            _cand("news", priority=2, urgency="info"),
            _cand("coach", priority=2, urgency="coaching"),
        ],
        make_snapshot(0.0),
    )
    assert {item.call.rule_id for item in d._queue} == {"hazard", "news"}
    dropped = {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}
    assert ("tip", "focus") in dropped
    assert ("coach", "focus") in dropped
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("systems", urgency="safety", tags=["systems"])], make_snapshot(0.0))
    d.submit([_cand("tip", urgency="info")], make_snapshot(1.0))
    assert {item.call.rule_id for item in d._queue} == {"systems", "tip"}


def test_location_coherence_after_safety() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("hazard", urgency="safety", tags=["systems"])], make_snapshot(0.0))
    d.drain(0.0)
    d.submit([_cand("corner", urgency="coaching", location_ref=True)], make_snapshot(1.0))
    assert d.drain(1.0) == []
    assert ("corner", "coherence") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_conflict_scores() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    lower = _cand("lower", conflict_group="choice")
    higher = _cand("higher", conflict_group="choice")
    lower.outcome_score, higher.outcome_score = 0.2, 0.9
    d.submit([lower], make_snapshot(0.0))
    d.submit([higher], make_snapshot(1.0))
    assert [item.call.rule_id for item in d._queue] == ["higher"]
    assert ("lower", "conflict_loser") in {
        (row["rule_id"], row["suppressed_by"]) for row in _log(buf)
    }


def test_contradicts_keeps_priority_then_class_then_newest() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("attack", priority=3, urgency="tactical")], make_snapshot(0.0))
    d.submit(
        [_cand("hazard", priority=1, urgency="safety", contradicts=["attack"])],
        make_snapshot(1.0),
    )
    assert [item.call.rule_id for item in d._queue] == ["hazard"]
    assert ("attack", "contradicted") in {(r["rule_id"], r["suppressed_by"]) for r in _log(buf)}

    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit(
        [_cand("hold", priority=2, urgency="tactical", contradicts=["news"])],
        make_snapshot(0.0),
    )
    d.submit([_cand("news", priority=2, urgency="info")], make_snapshot(1.0))
    assert [item.call.rule_id for item in d._queue] == ["hold"]
    assert ("news", "contradicted") in {(r["rule_id"], r["suppressed_by"]) for r in _log(buf)}

    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.submit(
        [_cand("first", priority=2, urgency="info", contradicts=["second"])],
        make_snapshot(0.0),
    )
    d.submit([_cand("second", priority=2, urgency="info")], make_snapshot(1.0))
    assert [item.call.rule_id for item in d._queue] == ["second"]


def test_rotation_is_seeded_and_both_rules_can_win() -> None:
    winners = set()
    for seed in range(1, 21):
        same_seed = []
        for _ in range(2):
            d, _, _ = _dispatcher(min_gap_s=0.0)
            d.reset_session(seed)
            d.submit(
                [_cand("a", rotate_with=["b"]), _cand("b", rotate_with=["a"])],
                make_snapshot(0.0),
            )
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
        d.submit([candidate], make_snapshot(float(index)))
    assert not any(item.call.rule_id == "target" for item in d._queue)
    assert ("target", "resolved") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_escalation_and_flush_drop_queued_calls() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("low", urgency="info")], make_snapshot(0.0))
    d.submit([_cand("high", urgency="safety", escalates=["low"])], make_snapshot(1.0))
    assert {item.call.rule_id for item in d._queue} == {"high"}
    assert ("low", "superseded_escalation") in {
        (row["rule_id"], row["suppressed_by"]) for row in _log(buf)
    }
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit([_cand("tip", urgency="info")], make_snapshot(0.0))
    d.menu_reply("Reply", "reply", make_snapshot(0.0))
    d.submit(
        [_cand("red", urgency="safety", flushes_queue=True, tags=["systems"])],
        make_snapshot(1.0),
    )
    assert {item.call.rule_id for item in d._queue} == {"red", "reply"}
    assert ("tip", "flushed") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_flush_calls_preserve_other_flush_calls_in_the_same_tick() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.submit(
        [
            _cand("ordinary", urgency="info"),
            _cand("finish_podium", urgency="info", flushes_queue=True),
            _cand("finish_gained", urgency="info", flushes_queue=True),
        ],
        make_snapshot(0.0),
    )

    assert {item.call.rule_id for item in d._queue} == {"finish_podium", "finish_gained"}
    assert ("ordinary", "flushed") in {(row["rule_id"], row["suppressed_by"]) for row in _log(buf)}


def test_obvious_and_provisional_suppression() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    obvious, provisional = _cand("obvious"), _cand("provisional")
    obvious.obvious = True
    provisional.provisional_silent = True
    d.submit([obvious, provisional], make_snapshot(0.0))
    assert not d._queue
    assert {row["suppressed_by"] for row in _log(buf)} == {"obvious", "provisional"}


def test_reply_absorption_is_topic_specific_and_reply_waits_for_execution() -> None:
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.menu_reply("Tyres are fine", "menu:tyres", make_snapshot(0.0), ["tyre_life"])
    d.submit([_cand("tyre_life", urgency="info")], make_snapshot(1.0))
    assert not any(item.call.rule_id == "menu:tyres" for item in d._queue)
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.menu_reply("Tyres are fine", "menu:tyres", make_snapshot(0.0), ["tyre_life"])
    d.submit([_cand("box_now", urgency="execution")], make_snapshot(1.0))
    assert any(item.call.rule_id == "menu:tyres" for item in d._queue)
    d, _, _ = _dispatcher(min_gap_s=0.0)
    d.menu_reply("Tyres are fine", "menu:tyres", make_snapshot(0.0), ["tyre_life"])
    d.submit([_cand("box", urgency="execution")], make_snapshot(1.0))
    assert [item.call.urgency for item in sorted(d._queue)] == ["execution", "reply"]
    assert {item.call.rule_id for item in d._queue} == {"box", "menu:tyres"}


def test_safety_cut_requeues_reply_once() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0)
    d.menu_reply(
        "A long answer that keeps speaking while safety arrives",
        "menu:answer",
        make_snapshot(0.0),
    )
    assert [call.rule_id for call in d.drain(0.0)] == ["menu:answer"]
    d.submit([_cand("hazard", urgency="safety", tags=["systems"])], make_snapshot(0.5))
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
    d.submit([_cand("slow", say_many=template)], make_snapshot(0.0))
    d.submit([_cand("slow", say_many=template)], make_snapshot(0.1))
    assert d.drain(0.1)[0].text == "two slow cars ahead. Careful"


def test_digest_uses_freshest_briefs_caps_items_and_skips_battle() -> None:
    d, _, buf = _dispatcher(min_gap_s=0.0, digest_max_items=2)
    for name, now in (("old", 0.0), ("newer", 1.0), ("newest", 2.0)):
        candidate = _cand(name, urgency="info")
        candidate.brief = name
        d.submit([candidate], make_snapshot(now))
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
    d.submit([_cand("tactical", urgency="tactical", text="T" * 90)], make_snapshot(0.0))
    assert [call.rule_id for call in d.drain(0.0)] == ["tactical"]
    for name in ("brief one", "brief two"):
        candidate = _cand(name.replace(" ", "_"), urgency="info")
        candidate.brief = name
        d.submit([candidate], make_snapshot(0.2))

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
