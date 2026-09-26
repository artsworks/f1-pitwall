from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.protocol.header import PacketId
from pitwall.rules.engine import Candidate

from .synth import make_event_packet, out_lap_scenario, pack_packet, write_packet_stream


def _read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_flashback_purges_queued_call(tmp_path: Path) -> None:
    log_path = tmp_path / "flbk.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    engine.ingest.on_datagram(pack_packet(PacketId.SESSION, {"session_type": 15}), 0.0)
    engine.ingest.on_datagram(
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"driver_status": 4}}},
            session_time=1.0,
        ),
        0.03,
    )
    # Queue a call without draining, then a flashback arrives: the listener
    # purges it and logs the suppression with reason "flashback".
    snap = engine.state.snapshot(0.03)
    assert engine.rule_engine is not None
    rule = next(r for r in engine.rule_engine.rules if r.id == "red_flag")
    cand = Candidate(
        rule=rule,
        text="test call",
        priority=1,
        tags=[],
        still_true=None,
        inputs={},
        trigger_t=0.0,
    )
    engine.dispatcher.submit([cand], snap)
    engine.ingest.on_datagram(make_event_packet(b"FLBK", session_time=1.2), 0.05)
    engine.dispatcher.log.flush()
    rows = _read_log(log_path)
    assert any(r["outcome"] == "suppressed" and r["suppressed_by"] == "flashback" for r in rows)
    assert engine.state.snapshot(0.05).rewinds == 1


def _red_flag_stream() -> list[tuple[float, bytes]]:
    dt = 1.0 / 30.0
    packets: list[tuple[float, bytes]] = []
    for i in range(90):  # 3 s
        t = i * dt
        packets.append(
            (
                t,
                pack_packet(PacketId.SESSION, {"session_type": 15}, session_time=t),
            )
        )
        packets.append(
            (
                t,
                pack_packet(
                    PacketId.LAP_DATA,
                    {
                        "cars": {
                            0: {
                                "driver_status": 4,
                                "pit_status": 0,
                                "current_lap_num": 2,
                            }
                        }
                    },
                    session_time=t,
                ),
            )
        )
        if i == 60:
            packets.append((t, make_event_packet(b"RDFL", session_time=t)))
    return packets


def test_red_flag_call_fires_once(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "rf.f1bin", _red_flag_stream())
    log_path = tmp_path / "rf.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    asyncio.run(run_replay(rec, engine, None))
    engine.dispatcher.log.flush()
    rows = _read_log(log_path)
    fired = [r for r in rows if r["outcome"] == "fired"]
    assert [r["rule_id"] for r in fired] == ["red_flag"]
    assert "Red flag" in fired[0]["text"]


def test_replay_deterministic_across_speeds(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "rf.f1bin", _red_flag_stream())
    logs = []
    for speed, name in ((None, "max"), (10.0, "10x")):
        log_path = tmp_path / f"{name}.jsonl"
        engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
        asyncio.run(run_replay(rec, engine, speed))
        engine.dispatcher.log.flush()
        logs.append(_read_log(log_path))
    strip = lambda rows: [{k: v for k, v in r.items() if k != "t"} for r in rows]  # noqa: E731
    assert strip(logs[0]) == strip(logs[1])


def _butn(down: bool, t: float, bit: int = 0x00100000) -> bytes:
    import struct

    status = bit if down else 0
    return pack_packet(
        PacketId.EVENT,
        {
            "event_string_code": b"BUTN",
            "event_data": struct.pack("<I", status).ljust(12, b"\0"),
        },
        session_time=t,
    )


def _quali_stream(
    rival_pos: float, t0: float, t1: float, lap: int = 1
) -> list[tuple[float, bytes]]:
    """Q1 garage frames at 5 Hz over [t0, t1): player in garage, rival flying
    at `rival_pos` lap distance on a 5 km track."""
    out: list[tuple[float, bytes]] = []
    dt = 0.2
    n = int((t1 - t0) / dt)
    for i in range(n):
        t = t0 + i * dt
        out.append(
            (
                t,
                pack_packet(
                    PacketId.SESSION,
                    {
                        "session_type": 5,
                        "session_time_left": 720,
                        "track_length": 5000,
                    },
                    session_time=t,
                ),
            )
        )
        out.append(
            (
                t,
                pack_packet(
                    PacketId.LAP_DATA,
                    {
                        "cars": {
                            0: {"driver_status": 0, "pit_status": 0, "current_lap_num": lap},
                            1: {
                                "driver_status": 1,
                                "pit_status": 0,
                                "result_status": 2,
                                "lap_distance": rival_pos,
                            },
                        }
                    },
                    session_time=t,
                ),
            )
        )
    return out


def test_release_hold_then_go(tmp_path: Path) -> None:
    # Rival positioned so it reaches pit exit just as the player would arrive
    # -> hold; then far away -> go. The rules share the "release" cooldown
    # group (30 s), so the clean phase must start after hold's cooldown.
    stream = _quali_stream(3400.0, 0.0, 31.0) + _quali_stream(2500.0, 31.0, 40.0)
    rec = write_packet_stream(tmp_path / "rel.f1bin", stream)
    log_path = tmp_path / "rel.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    asyncio.run(run_replay(rec, engine, None))
    engine.dispatcher.log.flush()
    rows = _read_log(log_path)
    fired = [r["rule_id"] for r in rows if r["outcome"] == "fired"]
    assert "release_hold" in fired
    assert "release_go" in fired
    assert fired.index("release_hold") < fired.index("release_go")


def _history_pkt(car_idx: int, lap_ms: int, t: float) -> bytes:
    return pack_packet(
        PacketId.SESSION_HISTORY,
        {
            "car_idx": car_idx,
            "num_laps": 1,
            "laps": {0: {"lap_time_ms": lap_ms, "lap_valid_bit_flags": 0x01}},
        },
        session_time=t,
    )


def test_abort_lap_fires_once(tmp_path: Path) -> None:
    stream: list[tuple[float, bytes]] = []
    # 20-car Q1: rivals' best laps make the cut-off 90.000 s (15th of 20).
    for i in range(1, 20):
        lap_ms = 89_000 if i <= 14 else (90_000 if i == 15 else 92_000)
        stream.append((i * 0.05, _history_pkt(i, lap_ms, i * 0.05)))
    # Player history: sectors valid (30 s each) but lap invalid -> best lap 0.
    stream.append(
        (
            1.0,
            pack_packet(
                PacketId.SESSION_HISTORY,
                {
                    "car_idx": 0,
                    "num_laps": 2,
                    "laps": {
                        0: {
                            "lap_time_ms": 91_500,
                            "lap_valid_bit_flags": 0x0E,
                            "sector1_ms_part": 30_000,
                            "sector2_ms_part": 30_000,
                            "sector3_ms_part": 30_000,
                        }
                    },
                },
                session_time=1.0,
            ),
        )
    )
    dt = 0.2
    for i in range(16):  # t = 0 .. 3.0
        t = i * dt
        stream.append(
            (
                t,
                pack_packet(
                    PacketId.SESSION,
                    {
                        "session_type": 5,
                        "session_time_left": 720,
                        "track_length": 5000,
                    },
                    session_time=t,
                ),
            )
        )
        stream.append(
            (
                t,
                pack_packet(
                    PacketId.PARTICIPANTS,
                    {"num_active_cars": 20},
                    session_time=t,
                ),
            )
        )
        # Flying, sector 1 done in 32.0 s, sector 2 in progress at 45 s lap.
        stream.append(
            (
                t,
                pack_packet(
                    PacketId.LAP_DATA,
                    {
                        "cars": {
                            0: {
                                "driver_status": 1,
                                "pit_status": 0,
                                "current_lap_num": 2,
                                "sector": 1,
                                "current_lap_time_ms": 45_000,
                                "sector1_time_ms_part": 32_000,
                            }
                        }
                    },
                    session_time=t,
                ),
            )
        )
    stream.sort(key=lambda p: p[0])
    rec = write_packet_stream(tmp_path / "ab.f1bin", stream)
    log_path = tmp_path / "ab.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    asyncio.run(run_replay(rec, engine, None))
    engine.dispatcher.log.flush()
    rows = _read_log(log_path)
    fired = [r for r in rows if r["outcome"] == "fired" and r["rule_id"] == "abort_lap"]
    assert len(fired) == 1
    assert "2.0" in fired[0]["text"]  # projected 92.0 vs 90.0 cutoff -> 2.0 s deficit


def test_butn_ack_and_neg(tmp_path: Path) -> None:
    # release_hold fires early; single BUTN press -> ack; double -> neg; the
    # rule is then suppressed with negative_backoff when it retriggers.
    stream = _quali_stream(3400.0, 0.0, 2.0)
    # single press at 1.0 -> ack ~1.45 s (outside the 350 ms double window)
    stream.append((1.0, _butn(True, 1.0)))
    stream.append((1.1, _butn(False, 1.1)))
    # double press at 1.6/1.7 + 1.9 -> neg on the second down
    stream.append((1.6, _butn(True, 1.6)))
    stream.append((1.7, _butn(False, 1.7)))
    stream.append((1.9, _butn(True, 1.9)))
    stream.append((2.0, _butn(False, 2.0)))
    stream += _quali_stream(2500.0, 2.0, 8.0)  # clean -> release_hold re-arms
    # same situation on lap 2 -> retrigger, but the neg muted the rule
    stream += _quali_stream(3400.0, 8.0, 14.0, lap=2)
    stream.sort(key=lambda p: p[0])
    rec = write_packet_stream(tmp_path / "butn.f1bin", stream)
    log_path = tmp_path / "butn.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    asyncio.run(run_replay(rec, engine, None))
    engine.dispatcher.log.flush()
    rows = _read_log(log_path)
    acks = [r for r in rows if r["outcome"] == "ack"]
    negs = [r for r in rows if r["outcome"] == "neg"]
    assert acks or negs  # see details below
    assert any(r["rule_id"] == "release_hold" for r in acks)
    assert any(r["rule_id"] == "release_hold" for r in negs)
    backoff = [
        r for r in rows if r["outcome"] == "suppressed" and r["suppressed_by"] == "negative_backoff"
    ]
    assert backoff and backoff[0]["rule_id"] == "release_hold"


def test_long_press_and_udp3_toggle_radio_silent(tmp_path: Path) -> None:
    stream = _quali_stream(2500.0, 0.0, 6.0)
    stream.append((1.0, _butn(True, 1.0)))  # UDP 1 held 1.2 s -> silent on
    stream.append((2.2, _butn(False, 2.2)))
    stream.append((4.0, _butn(True, 4.0, bit=0x00400000)))  # UDP 3 tap -> silent off
    stream.append((4.1, _butn(False, 4.1, bit=0x00400000)))
    stream.sort(key=lambda p: p[0])
    rec = write_packet_stream(tmp_path / "silent.f1bin", stream)
    log_path = tmp_path / "silent.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    asyncio.run(run_replay(rec, engine, None))
    engine.dispatcher.log.flush()
    outcomes = [r["outcome"] for r in _read_log(log_path)]
    assert [o for o in outcomes if o.startswith("silent")] == ["silent_on", "silent_off"]
    assert "bookmark" not in outcomes and "ack" not in outcomes
    assert engine.dispatcher.silent is False


def test_replay_writes_calls_and_laps_to_db(tmp_path: Path) -> None:
    from pitwall.store.db import Database

    rec = write_packet_stream(tmp_path / "db.f1bin", out_lap_scenario())
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(rec, engine, None))
    uid = engine.state.session_uid
    assert uid is not None
    calls = db.calls_for_session(uid)
    assert any(r["outcome"] == "fired" for r in calls)
    assert db._rows("SELECT * FROM sessions WHERE uid=?", (uid,))  # noqa: SLF001


def test_fired_record_has_call_id_and_inputs(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "ci.f1bin", out_lap_scenario())
    log_path = tmp_path / "ci.jsonl"
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=log_path)
    asyncio.run(run_replay(rec, engine, None))
    engine.dispatcher.log.flush()
    rows = _read_log(log_path)
    fired = [r for r in rows if r["outcome"] == "fired"]
    assert fired
    assert all(r.get("call_id") for r in fired)
    assert all(r.get("inputs") and "phase" in r["inputs"] for r in fired)
    queued = [r for r in rows if r["outcome"] == "queued"]
    assert queued and all(r.get("call_id") for r in queued)


def test_review_grade_end_to_end(tmp_path: Path) -> None:
    import io as _io

    from fastapi.testclient import TestClient

    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics
    from pitwall.server.app import create_app
    from pitwall.server.hub import Hub
    from pitwall.server.review import ReviewController
    from pitwall.store.db import Database

    rec = write_packet_stream(tmp_path / "gr.f1bin", out_lap_scenario())

    def factory(clk):  # type: ignore[no-untyped-def]
        return build_engine(
            clock=clk, decision_log_fp=_io.StringIO(), sinks=[], db=Database(":memory:")
        )

    ctl = ReviewController(rec, factory, Hub(), speed=10.0)
    app = create_app(Hub(), ConfigStore(), Metrics(), latest_snapshot=lambda: None, review=ctl)
    client = TestClient(app)
    tl = client.get("/api/review/timeline").json()
    fired = next(d for d in tl["decisions"] if d["outcome"] == "fired")
    assert fired["call_id"]
    # missing call_id -> 400, not 500
    assert client.post("/api/review/grade", content="{}").status_code == 400
    r = client.post(
        "/api/review/grade",
        content=json.dumps(
            {
                "call_id": fired["call_id"],
                "rule_id": fired["rule_id"],
                "grade": "good",
            }
        ),
    )
    assert r.status_code == 200
    grades = client.get("/api/review/grades").json()
    assert any(g["call_id"] == fired["call_id"] for g in grades)
    assert ctl.grades_path.exists()
