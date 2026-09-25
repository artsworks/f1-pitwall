from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay

from .synth import out_lap_scenario, write_packet_stream


def _replay(tmp_path: Path, speed: float | None, rec_path: Path) -> tuple[list[object], list[dict]]:
    sink_buf: list[str] = []

    class Collect:
        def speak(self, call) -> None:  # type: ignore[no-untyped-def]
            sink_buf.append(call.text)

        def cancel(self, call_id: str) -> None:
            pass

    log_path = tmp_path / f"d-{speed}.jsonl"
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[Collect()],
        decision_log_path=log_path,
    )
    delivered, calls = asyncio.run(run_replay(rec_path, engine, speed))
    engine.dispatcher.log.flush()
    return calls, [json.loads(line) for line in log_path.read_text().splitlines() if line]


def test_out_lap_cold_fires_once(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "out.f1bin", out_lap_scenario())
    calls, _ = _replay(tmp_path, None, rec)
    fired = [c.text for c in calls]
    assert len(fired) == 1
    assert "Tyres still coming in. front left coldest at 60" in fired[0]
    assert "60" in fired[0]


def test_deterministic_across_speeds(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "out.f1bin", out_lap_scenario())
    _, log_max = _replay(tmp_path, None, rec)
    _, log_10x = _replay(tmp_path, 10.0, rec)

    def strip(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "t"} for r in rows]

    assert strip(log_max) == strip(log_10x)
    fired = [r for r in log_max if r["outcome"] == "fired"]
    assert len(fired) == 1
    assert fired[0]["rule_id"] == "out_lap_s3_tyres_cold"
    assert fired[0]["mindset"] == "balanced"


def test_engine_replay_delivered(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "out.f1bin", out_lap_scenario())
    engine = build_engine(clock=VirtualClock(), sinks=[], decision_log_path=tmp_path / "d2.jsonl")
    delivered, calls = asyncio.run(run_replay(rec, engine, None))
    assert delivered == len(out_lap_scenario())
