from __future__ import annotations

import sys
import threading
import time
import xml.etree.ElementTree as ET

import pytest

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.voice.channel import CLOSED, OPEN, ChannelRecord, VoiceChannel
from pitwall.voice.grammar import SRGS_NS, VoiceGrammar, lang_for_lcid, normalise
from pitwall.voice.spike import format_row, record_row
from pitwall.voice.worker import EventSink, StaRecognizer, VoiceWorker


class FakeRec:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


def _channel() -> tuple[VoiceChannel, FakeRec, list[ChannelRecord]]:
    grammar = VoiceGrammar.from_mapping({"pit": ["should I pit"], "fuel": ["fuel", "fuel check"]})
    rec = FakeRec()
    channel = VoiceChannel(rec, grammar.intent_for, max_open_s=6.0, confidence_min=0.7)
    closed: list[ChannelRecord] = []
    channel.on_close = closed.append
    return channel, rec, closed


def test_packaged_grammar_loads_and_is_valid_srgs() -> None:
    voice = ConfigStore().current().voice
    grammar = VoiceGrammar.from_mapping(voice.intents)
    root = ET.fromstring(grammar.to_srgs("en-GB"))
    assert root.tag == f"{{{SRGS_NS}}}grammar"
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "en-GB"
    items = [item.text for item in root.iter(f"{{{SRGS_NS}}}item")]
    assert items == grammar.phrases and len(items) >= 20
    assert grammar.intent_for("What's the gap?") == "fight"
    assert grammar.intent_for("Copy that.") == "ack"
    menu_ids = {item.id for item in ConfigStore().current().menu.items}
    assert {"tyres", "pit", "fight", "rain", "push"} <= menu_ids & set(grammar.intents)
    assert {"understeer", "oversteer", "budget", "mindset"} <= menu_ids & set(grammar.intents)
    assert not {"gap", "gap_behind", "fuel", "plan", "race_stat"} & set(grammar.intents)


def test_grammar_rejects_duplicate_phrase() -> None:
    with pytest.raises(ValueError, match="both"):
        VoiceGrammar.from_mapping({"a": ["Fuel"], "b": ["fuel!"]})


def test_normalise_and_lcid() -> None:
    assert normalise("  How\u2019s the FUEL?? ") == "how's the fuel"
    assert lang_for_lcid("809;9") == "en-GB"
    assert lang_for_lcid("ffff") == "en-US"


def test_open_recognise_closes_with_intent_and_via() -> None:
    channel, rec, closed = _channel()
    assert channel.open(0.0, via="menu")
    assert channel.open(0.1, via="key") is False
    assert channel.state == OPEN and rec.calls == ["start"]
    channel.mark("sound_start", 0.2)
    channel.recognised("should I pit", 0.9, 1.0)
    assert channel.state == CLOSED and rec.calls == ["start", "stop"]
    (record,) = closed
    assert (record.reason, record.intent, record.via, record.cap_s) == (
        "recognised",
        "pit",
        "menu",
        6.0,
    )
    assert record.events["sound_start"] == pytest.approx(0.2)


def test_key_tap_toggles_and_closes_with_tap_reason() -> None:
    channel, rec, closed = _channel()
    channel.key_tap(0.0)
    assert channel.is_open and rec.calls == ["start"]
    channel.key_tap(0.5)
    assert not channel.is_open and rec.calls == ["start", "stop"]
    assert closed[0].reason == "tap"
    assert closed[0].via == "key"


def test_per_open_cap_and_default_cap() -> None:
    channel, _, closed = _channel()
    channel.key_tap(0.0, max_open_s=2.0)
    assert channel.current is not None and channel.current.cap_s == 2.0
    channel.tick(1.9)
    assert channel.is_open
    channel.tick(2.0)
    assert not channel.is_open and closed[-1].reason == "cap"

    channel.open(10.0, via="menu")
    assert channel.current is not None and channel.current.cap_s == 6.0
    channel.tick(16.0)
    assert closed[-1].reason == "cap"


def test_close_while_closed_does_not_stop_recognizer() -> None:
    channel, rec, closed = _channel()
    channel.close(1.0, "menu")
    assert rec.calls == [] and closed == []


def test_miss_low_confidence_and_out_of_grammar() -> None:
    channel, _, closed = _channel()
    channel.open(0.0)
    channel.false_recognition("", 0.0, 1.0)
    channel.open(2.0)
    channel.recognised("should I pit", 0.4, 3.0)
    channel.open(4.0)
    channel.recognised("not in grammar", 0.9, 5.0)
    assert [(record.reason, record.intent) for record in closed] == [
        ("miss", None),
        ("low_confidence", "pit"),
        ("miss", None),
    ]


def test_results_and_marks_while_closed_are_ignored() -> None:
    channel, _, closed = _channel()
    channel.recognised("fuel", 0.9, 1.0)
    channel.false_recognition("fuel", 0.9, 1.0)
    channel.mark("sound_start", 1.0)
    assert not closed and channel.current is None


class FakeStaRecognizer:
    def __init__(self, sink: EventSink, thread_ids: list[int]) -> None:
        self.sink = sink
        self.thread_ids = thread_ids
        self.listening = False
        self.emitted = False
        self._note_thread()

    def _note_thread(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def start(self) -> None:
        self._note_thread()
        self.listening = True

    def stop(self) -> None:
        self._note_thread()
        self.listening = False

    def pump(self) -> None:
        self._note_thread()
        if self.listening and not self.emitted:
            self.emitted = True
            self.sink("recognised", "fuel check", 0.9, time.monotonic())

    def close(self) -> None:
        self._note_thread()


def test_worker_keeps_recognizer_calls_on_its_daemon_thread() -> None:
    thread_ids: list[int] = []
    worker = VoiceWorker(lambda sink: FakeStaRecognizer(sink, thread_ids), com=False)
    assert worker.wait_ready(1.0)
    assert worker._thread.name == "voice" and worker._thread.daemon
    worker.start()
    deadline = time.monotonic() + 1.0
    events = []
    while time.monotonic() < deadline and not events:
        events.extend(worker.drain())
        threading.Event().wait(0.001)
    assert events and events[0][:3] == ("recognised", "fuel check", 0.9)
    worker.stop()
    worker.close()
    assert not worker._thread.is_alive()
    assert thread_ids and set(thread_ids) == {worker._thread.ident}
    assert worker._thread.ident != threading.get_ident()


def test_worker_factory_failure_sets_error_and_ready() -> None:
    def fail(_sink: EventSink) -> StaRecognizer:
        raise ValueError("cannot build recognizer")

    worker = VoiceWorker(fail, com=False)
    assert worker.wait_ready(1.0) is False
    assert isinstance(worker.error, ValueError)
    worker.close()
    assert not worker._thread.is_alive()


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: list[tuple[str, str, float, float]] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")

    def drain(self) -> list[tuple[str, str, float, float]]:
        events, self.events = self.events, []
        return events


def _engine() -> tuple[object, VirtualClock]:
    from pitwall.engine import build_engine

    clock = VirtualClock()
    engine = build_engine(
        clock=clock,
        sinks=[],
        overrides={"voice": {"enabled": True}},
    )
    return engine, clock


def test_engine_menu_opens_and_closes_voice() -> None:
    engine, _ = _engine()
    worker = FakeWorker()
    engine.enable_voice(worker)
    events: list[dict[str, object]] = []
    engine.on_voice_event = events.append

    engine.client_message({"type": "menu", "op": "down"})
    engine.tick(0.0)
    assert worker.calls == ["start"]
    assert events[0] == {"state": "listening", "via": "menu", "cap_s": 6.0}

    engine.client_message({"type": "menu", "op": "close"})
    engine.tick(0.0)
    assert worker.calls == ["start", "stop"]
    assert events[-1]["reason"] == "menu"


def test_engine_recognised_menu_intent_answers_and_closes_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _ = _engine()
    worker = FakeWorker()
    engine.enable_voice(worker)
    answers: list[tuple[str, str]] = []
    monkeypatch.setattr(
        engine,
        "_menu_answer",
        lambda item, _t, _snapshot, via: answers.append((item.id, via)),
    )
    engine.client_message({"type": "menu", "op": "down"})
    engine.tick(0.0)
    worker.events.append(("recognised", "should I pit", 0.9, time.monotonic()))
    engine.tick(0.5)
    assert answers == [("pit", "voice")]
    assert not engine.menu.open


def test_engine_miss_closes_voice_but_leaves_menu_open() -> None:
    engine, _ = _engine()
    worker = FakeWorker()
    engine.enable_voice(worker)
    engine.client_message({"type": "menu", "op": "down"})
    engine.tick(0.0)
    worker.events.append(("false", "unrecognised words", 0.0, time.monotonic()))
    engine.tick(0.5)
    assert not engine.voice.is_open
    assert engine.menu.open
    assert engine._voice_closed == []


def test_engine_v_toggle_has_two_second_cap() -> None:
    engine, clock = _engine()
    worker = FakeWorker()
    engine.enable_voice(worker)
    events: list[dict[str, object]] = []
    engine.on_voice_event = events.append
    engine.client_message({"type": "voice"})
    assert worker.calls == ["start"]
    assert engine.voice_payload(0.0)["left_s"] == 2.0
    clock.advance(2.0)
    engine.tick(clock.now())
    assert worker.calls == ["start", "stop"]
    assert events[-1]["reason"] == "cap"


def test_action_1_press_still_reaches_dispatcher_with_voice_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _ = _engine()
    worker = FakeWorker()
    engine.enable_voice(worker)
    presses: list[str] = []
    monkeypatch.setattr(
        engine.dispatcher,
        "on_press",
        lambda press, _snapshot: presses.append(press.kind),
    )
    engine._on_press_edge(0.0, True)
    engine._on_press_edge(0.1, False)
    engine.tick(0.45)
    assert presses == ["ack"]
    assert worker.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behavior")
def test_voice_setup_is_disabled_without_windows_sapi(
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, _ = _engine()
    assert engine.setup_voice() == "off (needs Windows)"
    assert engine.voice is None
    engine.client_message({"type": "voice"})
    assert engine.voice is None
    assert "voice: needs Windows SAPI; disabled" in capsys.readouterr().err


def test_spike_row_records_via_and_format() -> None:
    record = ChannelRecord(
        opened_t=0.0,
        closed_t=1.5,
        reason="recognised",
        text="fuel",
        intent="fuel",
        via="key",
    )
    record.confidence = 0.9
    record.events["sound_end"] = 1.2
    row = record_row(record, 80.0, 120.0)
    assert row["finalise_ms"] == pytest.approx(300.0)
    assert row["via"] == "key"
    assert "(key)" in format_row(row)
