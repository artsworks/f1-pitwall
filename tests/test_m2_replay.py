from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.protocol.header import PacketId
from pitwall.rules.engine import Candidate

from .synth import make_event_packet, pack_packet, write_packet_stream


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
