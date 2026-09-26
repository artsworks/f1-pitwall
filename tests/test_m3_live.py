"""M3 H4: UDP Action 2 mindset / Action 4 page, dashboard control messages,
auto page policy, --mask-restricted, pitwall tune, crash recovery."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.net.mask import mask_restricted
from pitwall.protocol.header import PacketId
from pitwall.protocol.packets import parse
from pitwall.store.db import Database
from pitwall.tune import load_cooldown_mults, record_diff, tune_from_db

from .race_synth import RaceSpec, race_stream
from .synth import pack_packet, write_packet_stream

MINDSET_BIT = 0x00200000
PAGE_BIT = 0x00800000


def _butn(status: int, t: float) -> bytes:
    return pack_packet(
        PacketId.EVENT,
        {"event_string_code": b"BUTN", "event_data": struct.pack("<I", status).ljust(12, b"\0")},
        session_time=t,
    )


def _press(stream: list[tuple[float, bytes]], bit: int, t: float) -> None:
    stream.append((t, _butn(bit, t)))
    stream.append((t + 0.1, _butn(0, t + 0.1)))
    stream.sort(key=lambda r: r[0])


def test_action4_cycles_page_and_action2_toggles_mindset(tmp_path: Path) -> None:
    stream = race_stream(RaceSpec(laps=3))
    _press(stream, PAGE_BIT, 20.0)
    _press(stream, PAGE_BIT, 25.0)
    _press(stream, MINDSET_BIT, 30.0)
    path = write_packet_stream(tmp_path / "r.f1bin", stream)
    log = tmp_path / "d.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log)
    assert engine.page == "race" and engine.mindset == "balanced"
    asyncio.run(run_replay(path, engine, None))
    engine.dispatcher.log.flush()
    assert engine.page == "car"  # race -> battle -> car
    assert engine.mindset == "aggressive"
    rows = [json.loads(x) for x in log.read_text().splitlines() if x]
    pages = [r["text"] for r in rows if r.get("outcome") == "page"]
    assert pages == ["battle", "car"]
    late = [r for r in rows if float(r.get("t") or 0) > 31.0]
    assert late and all(r["mindset"] == "aggressive" for r in late)


def test_action_bits_disableable(tmp_path: Path) -> None:
    stream = race_stream(RaceSpec(laps=2))
    _press(stream, PAGE_BIT, 20.0)
    _press(stream, MINDSET_BIT, 22.0)
    path = write_packet_stream(tmp_path / "r.f1bin", stream)
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        overrides={"input": {"page_cycle_bit": 0, "mindset_toggle_bit": 0}},
    )
    asyncio.run(run_replay(path, engine, None))
    assert engine.page == "race" and engine.mindset == "balanced"


def test_client_messages_select_page_and_mindset() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[])
    engine.client_message({"type": "page", "name": "track"})
    assert engine.page == "track"
    engine.client_message({"type": "page", "name": "nope"})
    assert engine.page == "track"
    engine.client_message({"type": "page", "cycle": True})
    assert engine.page == "setup"
    engine.client_message({"type": "mindset", "name": "aggressive"})
    assert engine.mindset == "aggressive"
    assert engine.dispatcher.log.mindset == "aggressive"
    engine.client_message({"type": "mindset"})
    assert engine.mindset == "balanced"


def test_auto_page_off_by_default_and_sc_switches_when_on(tmp_path: Path) -> None:
    spec = RaceSpec(laps=8, sc_laps=(4, 5))
    path = write_packet_stream(tmp_path / "r.f1bin", race_stream(spec))
    off = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=tmp_path / "a.jsonl")
    asyncio.run(run_replay(path, off, None))
    off.dispatcher.log.flush()
    assert '"outcome": "page"' not in (tmp_path / "a.jsonl").read_text()

    log = tmp_path / "b.jsonl"
    on = build_engine(
        clock=VirtualClock(),
        sinks=[],
        decision_log_path=log,
        overrides={"ui": {"auto_page": True, "auto_page_call_hold_s": 0}},
    )
    asyncio.run(run_replay(path, on, None))
    on.dispatcher.log.flush()
    pages = [
        json.loads(x)["text"] for x in log.read_text().splitlines() if '"outcome": "page"' in x
    ]
    assert "track" in pages


def test_mask_restricted_zeroes_rival_fields_only() -> None:
    cars = {
        i: {"fuel_in_tank": 20.0 + i, "ers_store_energy": 1e6, "tyres_age_laps": 3}
        for i in range(22)
    }
    status = pack_packet(PacketId.CAR_STATUS, {"cars": cars}, player=2)
    wear = {i: {"tyres_wear": (10.0, 11.0, 12.0, 13.0)} for i in range(22)}
    damage = pack_packet(PacketId.CAR_DAMAGE, {"cars": wear}, player=2)
    s1 = parse(PacketId.CAR_STATUS, mask_restricted(status))
    assert s1.cars[2].fuel_in_tank == 22.0 and s1.cars[2].ers_store_energy == 1e6
    rivals = [i for i in range(22) if i != 2]
    assert all(s1.cars[i].fuel_in_tank == 0 for i in rivals)
    assert all(s1.cars[i].ers_store_energy == 0 for i in rivals)
    assert all(s1.cars[i].tyres_age_laps == 3 for i in rivals)  # not restricted
    d1 = parse(PacketId.CAR_DAMAGE, mask_restricted(damage))
    assert max(d1.cars[2].tyres_wear.as_tuple()) == 13.0
    assert all(max(d1.cars[i].tyres_wear.as_tuple()) == 0 for i in rivals)
    lap = pack_packet(PacketId.LAP_DATA, {})
    assert mask_restricted(lap) == lap
    assert mask_restricted(b"xx") == b"xx"


def test_replay_mask_restricted_degrades_gracefully(tmp_path: Path) -> None:
    path = write_packet_stream(tmp_path / "r.f1bin", race_stream(RaceSpec(laps=8)))
    engine = build_engine(clock=VirtualClock(), sinks=[], db=Database(":memory:"))
    engine.ingest.transform = mask_restricted
    _, calls = asyncio.run(run_replay(path, engine, None))
    snap = engine.state.snapshot(engine.clock.now())
    assert snap.rival_data_restricted
    assert snap.laps_of_pace is not None  # own model unaffected


def test_tune_folds_grades_and_ab(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.sqlite")
    for i in range(3):
        db.grade_call(1, f"n{i}", "noisy", "noise", "")
        db.grade_call(1, f"g{i}", "useful", "good", "")
    db.grade_call(1, "x", "rare", "noise", "")
    rows = {r.rule_id: r for r in tune_from_db(db, {})}
    assert rows["noisy"].cooldown_mult == 4.0
    assert rows["useful"].cooldown_mult == 0.5
    assert rows["rare"].cooldown_mult == 1.0  # below tune_min_grades
    mults = load_cooldown_mults(db)
    assert mults["noisy"] == 4.0
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    assert engine.dispatcher.tuned_cooldown["noisy"] == 4.0

    result = {
        "files": [
            {
                "recording": "r.f1bin",
                "only_a": [{"rule_id": "noisy"}, {"rule_id": "noisy"}],
                "only_b": [],
                "summary": {"per_rule": {"noisy": {"a": 5, "b": 3}}},
            }
        ]
    }
    assert record_diff(db, result, a_dir="a", b_dir="b") == 1
    rows = {r.rule_id: r for r in tune_from_db(db, {})}
    assert rows["noisy"].ab_net == -2
    assert tune_from_db(db, {}) == list(rows.values())  # idempotent


def test_crash_recovery_rebuilds_from_db_and_recording_tail(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "r.f1bin", race_stream(RaceSpec(laps=6)))
    dbp = tmp_path / "p.sqlite"
    first = build_engine(clock=VirtualClock(), sinks=[], db=Database(dbp))
    asyncio.run(run_replay(rec, first, None))
    uid = first.state.session_uid
    assert uid is not None
    lap = first.state.lap_num
    db = Database(dbp)
    n_laps = len(db.laps_for(uid, 0))
    assert n_laps > 0
    db.write_heartbeat(uid, 0.0, time.time(), str(rec), lap)

    second = build_engine(clock=VirtualClock(), sinks=[], db=db)
    msg = second.recover()
    assert msg is not None and second.state.lap_num == lap
    assert second.state.session_uid == uid
    assert len(db.laps_for(uid, 0)) == n_laps  # nothing re-inserted

    stale = build_engine(clock=VirtualClock(), sinks=[], db=db)
    assert stale.recover(wall_now=time.time() + 10_000) is None
