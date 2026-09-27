"""Driver -> pit wall menu (docs/12): navigation, timeout, confirm, replies,
persistence, disabled bits and replay determinism."""

from __future__ import annotations

import asyncio
import json
import math
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError

from pitwall.audio.dispatcher import Call
from pitwall.clock import VirtualClock
from pitwall.config.models import InputSettings, MenuItemModel, MenuSettings
from pitwall.engine import Engine, build_engine, run_replay
from pitwall.input.menu import DriverMenu, ReplyPicker, answer, validate, validate_shortcuts
from pitwall.protocol.header import PacketId
from pitwall.state.session import Snapshot
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import pack_packet, write_packet_stream

ACK = 0x00100000
UP = 0x00200000
DOWN = 0x00400000
CLOSE = 0x04000000
RIGHT = 0x00800000  # page cycle; confirms while the menu is open
LEFT = 0x01000000  # mindset; closes while the menu is open
SD6, SD7, SD8 = 0x02000000, 0x04000000, 0x08000000


def _butn(status: int, t: float) -> bytes:
    return pack_packet(
        PacketId.EVENT,
        {"event_string_code": b"BUTN", "event_data": struct.pack("<I", status).ljust(12, b"\0")},
        session_time=t,
    )


def _stream(presses: list[tuple[int, float]], laps: int = 3) -> list[tuple[float, bytes]]:
    stream = race_stream(RaceSpec(laps=laps))
    for bit, t in presses:
        stream.append((t, _butn(bit, t)))
        stream.append((t + 0.1, _butn(0, t + 0.1)))
    stream.sort(key=lambda r: r[0])
    return stream


def _run(
    tmp_path: Path,
    presses: list[tuple[int, float]],
    *,
    name: str = "m",
    overrides: dict[str, object] | None = None,
    db: Database | None = None,
) -> tuple[Engine, list[Call], list[dict[str, object]]]:
    path = write_packet_stream(tmp_path / f"{name}.f1bin", _stream(presses))
    log = tmp_path / f"{name}.jsonl"
    engine = build_engine(
        clock=VirtualClock(), sinks=[], decision_log_path=log, overrides=overrides, db=db
    )
    _, calls = asyncio.run(run_replay(path, engine, None))
    engine.dispatcher.log.flush()
    rows = [json.loads(x) for x in log.read_text().splitlines() if x]
    return engine, calls, rows


def _inputs(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [r for r in rows if r.get("outcome") == "driver_input"]


def test_down_opens_on_first_item_and_scrolls() -> None:
    settings = MenuSettings(
        items=[MenuItemModel(id=i, label=i.title()) for i in ("tyres", "pit", "gap")]
    )
    menu = DriverMenu()
    first = menu.step(settings, 1, 1.0)
    assert first is not None and first.id == "tyres" and menu.open
    menu.step(settings, 1, 1.5)
    item = menu.step(settings, 1, 2.0)
    assert item is not None and item.id == "gap"
    item = menu.step(settings, 1, 2.5)  # wraps
    assert item is not None and item.id == "tyres"
    assert menu.payload(settings, 3.0) == {
        "open": True,
        "index": 0,
        "items": ["Tyres", "Pit", "Gap"],
        "left_s": 5.5,
        "timeout_s": 6.0,
    }
    assert not menu.expired(settings, 8.4) and menu.expired(settings, 8.5)
    closed = DriverMenu()
    last = closed.step(settings, -1, 0.0)
    assert last is not None and last.id == "gap"
    no_wrap = MenuSettings(wrap=False, items=settings.items)
    menu = DriverMenu()
    menu.step(no_wrap, -1, 0.0)
    item = menu.step(no_wrap, 1, 0.1)
    assert item is not None and item.id == "gap"
    assert DriverMenu().step(MenuSettings(enabled=False, items=settings.items), 1, 0) is None


def test_replay_navigation_and_timeout(tmp_path: Path) -> None:
    engine, calls, rows = _run(tmp_path, [(DOWN, 20.0), (DOWN, 21.0), (UP, 22.0)])
    outcomes = [r["outcome"] for r in rows if str(r["outcome"]).startswith("menu")]
    assert outcomes == ["menu_open", "menu_close"]
    close = next(r for r in rows if r["outcome"] == "menu_close")
    assert close["text"] == "timeout" and 28.0 <= float(str(close["t"])) < 29.0
    assert not _inputs(rows) and not engine.menu.open
    assert not [c for c in calls if "menu" in c.tags]  # prompts are audio only


def test_confirm_answers_from_snapshot_and_skips_ack(tmp_path: Path) -> None:
    engine, calls, rows = _run(tmp_path, [(DOWN, 20.0), (DOWN, 21.0), (ACK, 22.0)])
    (rec,) = _inputs(rows)
    assert rec["item_id"] == "pit" and rec["kind"] == "question"
    assert rec["rule_id"] == "menu:pit" and not engine.menu.open
    menu = engine.store.current().menu
    pit = next(i for i in menu.items if i.id == "pit")
    case = str(dict(rec["inputs"])["case"])  # type: ignore[call-overload]
    assert case in pit.replies
    reply = next(c for c in calls if c.rule_id == "menu:pit")
    assert reply.priority == 1 and "menu_answer" in reply.tags and reply.text == rec["text"]
    assert not [r for r in rows if r["outcome"] in ("ack", "neg", "say_again", "quiet_until")]


def test_double_press_and_close_button_cancel(tmp_path: Path) -> None:
    presses = [(DOWN, 20.0), (ACK, 21.0), (ACK, 21.2), (UP, 24.0), (CLOSE, 25.0)]
    close_bit: dict[str, object] = {"input": {"menu_close_bit": CLOSE, "shortcuts": []}}
    engine, _, rows = _run(tmp_path, presses, overrides=close_bit)
    closes = [r["text"] for r in rows if r["outcome"] == "menu_close"]
    assert closes == ["cancel", "close"]
    assert not _inputs(rows) and engine.dispatcher.quiet_until is None


def test_opinion_persisted_and_biases_snapshot(tmp_path: Path) -> None:
    db = Database(":memory:")
    # Up opens on the last item (page); four more Ups reach "Understeer".
    presses = [(UP, 20.0 + 0.5 * i) for i in range(5)] + [(ACK, 23.0)]
    engine, calls, rows = _run(tmp_path, presses, db=db)
    (rec,) = _inputs(rows)
    assert rec["item_id"] == "understeer" and rec["topic"] == "balance"
    uid = engine.state.session_uid
    assert uid is not None
    (row,) = db.driver_inputs_for_session(uid)
    assert row["item_id"] == "understeer" and row["kind"] == "opinion"
    assert row["reply"] == rec["text"] and "understeer" in str(row["reply"]).lower()
    assert engine.opinions["balance"][0] == "understeer"
    lap = int(str(rec["lap"]))
    assert engine.driver_balance(lap) == "understeer"
    assert engine.driver_balance(lap + 6) == ""
    assert any(c.rule_id == "menu:understeer" for c in calls)


def test_action_items_run_their_action(tmp_path: Path) -> None:
    presses = [(UP, 20.0), (UP, 20.5), (UP, 21.0), (ACK, 22.0)]  # page, silent, mindset
    engine, _, rows = _run(tmp_path, presses)
    (rec,) = _inputs(rows)
    assert rec["item_id"] == "mindset" and engine.mindset == "aggressive"
    engine2, _, _ = _run(tmp_path, [(UP, 20.0), (UP, 20.5), (ACK, 22.0)], name="s")
    assert engine2.dispatcher.silent is True


def test_budget_action_cycles_calls_per_lap(tmp_path: Path) -> None:
    # page, silent, mindset, oversteer, understeer, budget
    presses = [(UP, 20.0 + 0.5 * i) for i in range(6)] + [(ACK, 23.5)]
    engine, calls, rows = _run(tmp_path, presses)
    (rec,) = _inputs(rows)
    assert rec["item_id"] == "budget"
    assert engine.budget_live == 20 and engine.dispatcher.budget_override == 20
    assert any(c.text == "Copy, up to 20 calls a lap." for c in calls)
    engine.cycle_budget()
    assert engine.dispatcher.budget_override == 4  # wraps
    engine.set_mindset("aggressive", 30.0)
    assert engine.dispatcher.budget_override == 4  # menu choice beats the mindset


def test_menu_bits_disableable(tmp_path: Path) -> None:
    overrides: dict[str, object] = {"input": {"menu_up_bit": 0, "menu_down_bit": 0}}
    engine, _, rows = _run(tmp_path, [(DOWN, 20.0), (ACK, 21.0)], overrides=overrides)
    assert not _inputs(rows) and not engine.menu.open
    assert not [r for r in rows if r["outcome"] == "menu_open"]
    off: dict[str, object] = {"menu": {"enabled": False}}
    engine, _, rows = _run(tmp_path, [(DOWN, 20.0)], name="off", overrides=off)
    assert not [r for r in rows if r["outcome"] == "menu_open"]


def test_replay_is_deterministic(tmp_path: Path) -> None:
    presses = [(DOWN, 20.0), (ACK, 21.0), (DOWN, 30.0), (DOWN, 30.5), (ACK, 31.0)]
    _, _, a = _run(tmp_path, presses, name="a")
    _, _, b = _run(tmp_path, presses, name="b")
    assert [(r["item_id"], r["text"]) for r in _inputs(a)] == [
        (r["item_id"], r["text"]) for r in _inputs(b)
    ]
    assert len(_inputs(a)) == 2


def test_client_menu_messages_and_state_payload() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[])
    engine.client_message({"type": "menu", "op": "down"})
    engine.client_message({"type": "menu", "op": "down"})
    engine.client_message({"type": "menu", "op": "bogus"})
    engine.tick(engine.clock.now())
    payload = engine.menu_payload(engine.clock.now())
    assert payload["open"] is True and payload["index"] == 1
    engine.client_message({"type": "menu", "op": "close"})
    engine.tick(engine.clock.now())
    assert engine.menu_payload(engine.clock.now()) == {"open": False}


def test_answers_and_reply_variants_rotate() -> None:
    item = MenuItemModel(
        id="gap",
        label="Gap ahead?",
        replies={"closing": ["{gap} to {name}, closing {trend}.", "{name}, {gap}."]},
    )
    snap = Snapshot(
        now=0.0, rival_ahead_idx=3, gap_ahead_s=1.43, gap_trend_ahead_s=0.31, rival_ahead_name="NOR"
    )
    case, values = answer(item, snap, "balanced")
    assert case == "closing"
    picker = ReplyPicker()
    assert picker.pick(item, case, values) == "1.4 to NOR, closing 0.3."
    assert picker.pick(item, case, values) == "NOR, 1.4."
    assert picker.pick(item, case, values) == "1.4 to NOR, closing 0.3."
    assert answer(item, Snapshot(now=0.0, gap_ahead_s=math.inf), "balanced")[0] == "none"
    fuel = MenuItemModel(id="fuel", label="Fuel OK?")
    short = Snapshot(now=0.0, fuel_source="measured", fuel_margin_laps=-0.5)
    case, values = answer(fuel, short, "balanced")
    assert case == "short" and values["margin"] == "0.5"
    under = MenuItemModel(id="understeer", label="Understeer", kind="opinion", topic="balance")
    assert answer(under, Snapshot(now=0.0, front_brake_bias=57), "b")[1]["bias_to"] == "56"


def test_validate_and_duplicate_bits() -> None:
    bad = MenuSettings(
        items=[
            MenuItemModel(id="x", label="X", replies={"default": ["{nope}"]}),
            MenuItemModel(id="x", label="Y", kind="action"),
            MenuItemModel(id="o", label="O", kind="opinion", replies={"default": ["ok"]}),
        ]
    )
    errors = validate(bad)
    assert any("unknown placeholder" in e for e in errors)
    assert any("duplicate id" in e for e in errors)
    assert any("needs `action`" in e for e in errors)
    assert any("needs a `topic`" in e for e in errors)
    with pytest.raises(ValidationError):
        InputSettings(menu_up_bit=0x00200000, mindset_toggle_bit=0x00200000)
    InputSettings(menu_up_bit=0, mindset_toggle_bit=0)  # zeros never collide


def test_packaged_menu_is_valid() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[])
    settings = engine.store.current()
    assert validate(settings.menu) == []
    labels = [i.label for i in settings.menu.items]
    assert labels[0] == "Tyres gone?" and labels[-1] == "Next page"
    assert all(len(label.split()) <= 3 for label in labels)


def test_stick_right_confirms_and_left_closes_while_open(tmp_path: Path) -> None:
    presses = [(DOWN, 20.0), (DOWN, 20.5), (RIGHT, 21.0), (DOWN, 24.0), (LEFT, 24.5)]
    engine, _, rows = _run(tmp_path, presses)
    (rec,) = _inputs(rows)
    assert rec["item_id"] == "pit"
    assert [r["text"] for r in rows if r["outcome"] == "menu_close"] == ["close"]
    assert engine.mindset == "balanced"
    assert not [r for r in rows if r["outcome"] == "page" and r.get("manual")]


def test_stick_right_left_keep_page_and_mindset_when_closed(tmp_path: Path) -> None:
    engine, _, rows = _run(tmp_path, [(RIGHT, 20.0), (LEFT, 21.0)])
    assert engine.mindset == "aggressive"
    assert [r for r in rows if r["outcome"] == "page" and r.get("manual")]
    assert not [r for r in rows if r["outcome"] == "menu_open"]


def test_stream_deck_shortcuts_answer_directly(tmp_path: Path) -> None:
    db = Database(":memory:")
    engine, calls, rows = _run(tmp_path, [(SD6, 20.0), (SD7, 25.0), (SD8, 30.0)], db=db)
    recs = _inputs(rows)
    assert [r["item_id"] for r in recs] == ["pit", "race_stat", "fight"]
    assert all(dict(r["inputs"])["via"] == "shortcut" for r in recs)  # type: ignore[call-overload]
    assert not [r for r in rows if r["outcome"] == "menu_open"] and not engine.menu.open
    for item in ("pit", "race_stat", "fight"):
        assert any(c.rule_id == f"menu:{item}" and c.priority == 1 for c in calls)
    uid = engine.state.session_uid
    assert uid is not None
    assert [r["item_id"] for r in db.driver_inputs_for_session(uid)] == [
        "pit",
        "race_stat",
        "fight",
    ]


def test_shortcut_while_menu_open_closes_it(tmp_path: Path) -> None:
    _, _, rows = _run(tmp_path, [(DOWN, 20.0), (SD7, 21.0)])
    assert [r["text"] for r in rows if r["outcome"] == "menu_close"] == ["shortcut"]
    assert [r["item_id"] for r in _inputs(rows)] == ["race_stat"]


def test_race_stat_and_fight_answers() -> None:
    stat = MenuItemModel(id="race_stat", label="Race stat")
    short = Snapshot(now=0.0, fuel_source="measured", fuel_margin_laps=-0.6, position=4)
    assert answer(stat, short, "b")[0] == "fuel_short"
    plain = Snapshot(now=0.0, position=4, laps_remaining=12, player_best_lap_ms=92_412)
    case, values = answer(stat, plain, "b")
    assert case == "position" and values["pos"] == "4" and values["best"] == "1:32.4"
    assert answer(stat, Snapshot(now=0.0), "b")[0] == "unknown"
    fight = MenuItemModel(id="fight", label="Fight")
    snap = Snapshot(
        now=0.0,
        laps_remaining=10,
        rival_ahead_idx=2,
        rival_ahead_name="Norris",
        gap_ahead_s=1.2,
        gap_trend_ahead_s=0.3,
        rival_ahead_pace_ms=92_700,
        base_pace_ms=92_400.0,
        rival_behind_idx=5,
        rival_behind_name="Russell",
        gap_behind_s=0.9,
        gap_trend_behind_s=-0.2,
    )
    case, values = answer(fight, snap, "b")
    assert case == "both"
    assert values["ahead"] == (
        "Norris 1.2 ahead, closing 0.3 a lap, catch in 4. Pace 1:32.4 to his 1:32.7."
    )
    assert values["behind"] == "Russell 0.9 behind, pulling away 0.2 a lap."
    assert answer(fight, Snapshot(now=0.0), "b")[0] == "none"


def test_shortcut_validation() -> None:
    with pytest.raises(ValidationError):
        InputSettings(shortcuts=[{"bit": 0x00200000, "item": "pit"}], menu_up_bit=0x00200000)
    with pytest.raises(ValidationError):
        InputSettings(shortcuts=[{"bit": 1, "item": "pit"}, {"bit": 2, "item": "pit"}])
    inp = InputSettings(shortcuts=[{"bit": 1, "item": "nope"}])
    menu = MenuSettings(items=[MenuItemModel(id="pit", label="Pit now?")])
    assert validate_shortcuts(inp, menu) == ["input.shortcuts: unknown menu item 'nope'"]
