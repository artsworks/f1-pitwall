from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.protocol.header import HEADER_SIZE, PacketId
from pitwall.voice.channel import CLOSED, OPEN, PROVISIONAL, ChannelRecord, VoiceChannel
from pitwall.voice.grammar import SRGS_NS, VoiceGrammar, lang_for_lcid, normalise
from pitwall.voice.spike import butn_status, format_row, record_row


class FakeRec:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


def _channel() -> tuple[VoiceChannel, FakeRec, list[ChannelRecord]]:
    g = VoiceGrammar.from_mapping({"gap": ["gap ahead"], "fuel": ["fuel", "fuel check"]})
    rec = FakeRec()
    ch = VoiceChannel(rec, g.intent_for, max_open_s=6.0, confidence_min=0.7)
    out: list[ChannelRecord] = []
    ch.on_close = out.append
    return ch, rec, out


def _tap(ch: VoiceChannel, t: float) -> None:
    ch.button_edge(t, True)
    ch.button_edge(t + 0.1, False)


def test_packaged_grammar_loads_and_is_valid_srgs() -> None:
    voice = ConfigStore().current().voice
    g = VoiceGrammar.from_mapping(voice.intents)
    root = ET.fromstring(g.to_srgs("en-GB"))
    assert root.tag == f"{{{SRGS_NS}}}grammar"
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "en-GB"
    items = [i.text for i in root.iter(f"{{{SRGS_NS}}}item")]
    assert items == g.phrases and len(items) >= 40
    assert g.intent_for("What's the gap?") == "gap"
    assert g.intent_for("Copy that.") == "ack"
    menu_ids = {i.id for i in ConfigStore().current().menu.items}
    assert {"tyres", "pit", "gap", "fuel", "fight"} <= menu_ids & set(g.intents)


def test_grammar_rejects_duplicate_phrase() -> None:
    with pytest.raises(ValueError, match="both"):
        VoiceGrammar.from_mapping({"a": ["Fuel"], "b": ["fuel!"]})


def test_normalise_and_lcid() -> None:
    assert normalise("  How\u2019s the FUEL?? ") == "how's the fuel"
    assert lang_for_lcid("809;9") == "en-GB"
    assert lang_for_lcid("ffff") == "en-US"


def test_single_tap_opens_on_down_edge_and_recognition_closes() -> None:
    ch, rec, out = _channel()
    ch.button_edge(0.0, True)
    assert ch.state == PROVISIONAL and rec.calls == ["start"]
    ch.button_edge(0.1, False)
    ch.tick(0.5)  # double window (350 ms) passed -> single tap
    assert ch.state == OPEN
    ch.mark("sound_start", 0.6)
    ch.recognised("gap ahead", 0.9, 1.4)
    assert ch.state == CLOSED and rec.calls == ["start", "stop"]
    (r,) = out
    assert (r.reason, r.intent, r.events["sound_start"]) == ("recognised", "gap", 0.6)


def test_result_before_single_tap_resolves_is_kept() -> None:
    ch, _, out = _channel()
    ch.button_edge(0.0, True)
    ch.button_edge(0.05, False)
    ch.recognised("fuel", 0.9, 0.3)
    assert ch.state == PROVISIONAL and not out
    ch.tick(0.45)
    assert ch.state == CLOSED and out[0].intent == "fuel"


def test_double_tap_aborts_as_negative() -> None:
    ch, rec, out = _channel()
    _tap(ch, 0.0)
    ch.button_edge(0.2, True)
    assert ch.state == CLOSED and rec.calls == ["start", "stop"]
    assert (out[0].reason, out[0].passthrough) == ("abort", "neg")
    ch.button_edge(0.3, False)
    ch.tick(1.0)
    assert len(out) == 1 and ch.state == CLOSED


def test_long_press_aborts_as_long_press() -> None:
    ch, _, out = _channel()
    ch.button_edge(0.0, True)
    ch.tick(0.9)
    assert ch.state == CLOSED and out[0].passthrough == "bookmark"
    ch.button_edge(1.5, False)
    assert ch.state == CLOSED


def test_second_tap_closes_and_its_release_is_swallowed() -> None:
    ch, _, out = _channel()
    _tap(ch, 0.0)
    ch.tick(0.5)
    ch.button_edge(2.0, True)
    assert ch.state == CLOSED and out[0].reason == "tap"
    ch.button_edge(2.1, False)
    ch.tick(3.0)
    assert ch.state == CLOSED and len(out) == 1


def test_hard_cap_miss_and_low_confidence() -> None:
    ch, _, out = _channel()
    ch.key_tap(0.0)
    ch.tick(6.0)
    ch.key_tap(10.0)
    ch.false_recognition("", 0.0, 11.0)
    ch.key_tap(20.0)
    ch.recognised("fuel check", 0.4, 21.0)
    ch.key_tap(30.0)
    ch.recognised("not in grammar", 0.9, 31.0)
    assert [(r.reason, r.intent) for r in out] == [
        ("cap", None),
        ("miss", None),
        ("low_confidence", "fuel"),
        ("miss", None),
    ]


def test_results_while_closed_are_ignored() -> None:
    ch, _, out = _channel()
    ch.recognised("fuel", 0.9, 1.0)
    assert not out


def test_butn_status_and_row() -> None:
    payload = bytearray(HEADER_SIZE + 8)
    payload[6] = PacketId.EVENT
    payload[HEADER_SIZE : HEADER_SIZE + 4] = b"BUTN"
    payload[HEADER_SIZE + 4 : HEADER_SIZE + 8] = (0x00100000).to_bytes(4, "little")
    assert butn_status(bytes(payload)) == 0x00100000
    payload[HEADER_SIZE : HEADER_SIZE + 4] = b"SCAR"
    assert butn_status(bytes(payload)) is None
    r = ChannelRecord(opened_t=0.0, closed_t=1.5, reason="recognised", text="fuel", intent="fuel")
    r.confidence = 0.9
    r.events["sound_end"] = 1.2
    row = record_row(r, 80.0, 120.0)
    assert row["finalise_ms"] == pytest.approx(300.0)
    assert "fuel" in format_row(row)
