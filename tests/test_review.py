from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pitwall.audio.dispatcher import Call
from pitwall.clock import PausableClock, VirtualClock
from pitwall.engine import Engine, build_engine, run_replay
from pitwall.metrics import Metrics
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.server.review import ReviewController
from pitwall.state.session import SessionState
from pitwall.store.db import Database

from .synth import out_lap_scenario, write_packet_stream


def _make_recording(tmp_path: Path) -> Path:
    return write_packet_stream(tmp_path / "r.f1bin", out_lap_scenario())


def test_pausable_clock_pauses() -> None:
    inner = VirtualClock()

    async def run() -> float:
        c = PausableClock(inner)
        c.pause()
        assert c.paused
        t0 = c.now()
        inner.advance(5.0)
        assert c.now() == t0  # paused time excluded
        c.resume()
        inner.advance(1.0)
        return c.now() - t0

    assert asyncio.run(run()) == 1.0


def _factory(clk) -> Engine:  # type: ignore[no-untyped-def]
    return build_engine(clock=clk, decision_log_fp=io.StringIO(), sinks=[], db=Database(":memory:"))


def test_timeline_prepass(tmp_path: Path) -> None:
    rec = _make_recording(tmp_path)
    ctl = ReviewController(rec, _factory, Hub(), speed=10.0)
    tl = ctl.timeline
    assert tl["duration_us"] > 0
    assert tl["decisions"], "expected fired/suppressed decision rows"
    assert tl["laps"], "expected lap index entries"


def test_seek_restarts_at_lap_boundary(tmp_path: Path) -> None:
    rec = _make_recording(tmp_path)
    ctl = ReviewController(rec, _factory, Hub(), speed=1e9)

    async def full() -> int:
        delivered, _ = await run_replay(rec, _factory(VirtualClock()), None)
        return delivered

    total = asyncio.run(full())
    assert ctl.timeline["laps"]

    async def seeked() -> None:
        await ctl.seek(lap=ctl.timeline["laps"][-1]["lap"])
        assert ctl._task is not None  # noqa: SLF001
        await ctl._task

    asyncio.run(seeked())
    # The seeked run covered strictly fewer datagrams than the full replay.
    assert 0 < ctl._task.result()[0] < total  # noqa: SLF001


def test_review_api_grade(tmp_path: Path) -> None:
    rec = _make_recording(tmp_path)
    ctl = ReviewController(rec, _factory, Hub(), speed=10.0)
    hub = Hub()
    state = SessionState()
    app = create_app(
        hub,
        __import__("pitwall.config.loader", fromlist=["ConfigStore"]).ConfigStore(),
        Metrics(),
        latest_snapshot=lambda: state.snapshot(0.0),
        review=ctl,
    )
    client = TestClient(app)
    st = client.get("/api/review/status").json()
    assert "playing" in st and "position_us" in st
    tl = client.get("/api/review/timeline").json()
    assert tl["decisions"]
    call_id = tl["decisions"][0].get("call_id") or "c-x"
    r = client.post(
        "/api/review/grade",
        content=json.dumps({"call_id": call_id, "rule_id": "x", "grade": "good"}),
    ).json()
    assert r["grade"] == "good"
    assert ctl.grades_path.exists()
    assert client.get("/api/review/grades").json()[0]["call_id"] == call_id


def test_replay_serve_streams_speaker_audio(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.speaker as speaker_module
    import pitwall.cli.recordings as cli_module
    from pitwall.cli import main

    class _Speaker:
        name = "fake-stream"
        speaks_audio = True
        supports_audio_stream = True
        on_spoken = None
        on_audio = None
        on_dropped = None

        def __init__(self) -> None:
            self.flushes = 0
            self.closed = False

        def speak(self, call) -> None:  # type: ignore[no-untyped-def]
            pass

        def cancel(self, call_id: str) -> None:
            pass

        def flush(self) -> None:
            self.flushes += 1

        def close(self) -> None:
            self.closed = True

    speaker = _Speaker()
    monkeypatch.setattr(speaker_module, "make_speaker", lambda _settings: speaker)
    seen: dict[str, object] = {}

    async def fake_serve(engine, hub, _store, coro, *, review) -> None:  # type: ignore[no-untyped-def]
        coro.close()
        seen["streams"] = hub.streams_audio
        seen["on_audio"] = speaker.on_audio == hub.audio
        seen["sinks"] = engine.dispatcher.sinks
        seen["speaker_name"] = engine.speaker_name
        first_call = Call(
            id="review-spoken",
            rule_id="test",
            priority=2,
            text="line",
            tags=[],
            deadline_ms=1000,
            lap=0,
            t=0.0,
            trigger_t=0.0,
        )
        hub.speak(first_call)
        handoff = time.time()
        engine.dispatcher.metrics.note_handoff(first_call.id, handoff)
        assert speaker.on_spoken is not None
        assert speaker.on_dropped is not None
        speaker.on_spoken(first_call.id, handoff + 0.1)
        seen["first_tts_ms"] = engine.dispatcher.metrics.summary()["tts_ms"]["p99"]
        drop_call = Call(
            id="review-dropped",
            rule_id="test",
            priority=2,
            text="drop",
            tags=[],
            deadline_ms=1000,
            lap=0,
            t=0.0,
            trigger_t=0.0,
        )
        hub.speak(drop_call)
        speaker.on_dropped(drop_call.id)
        flushes = speaker.flushes
        rebuilt = review._engine_factory(VirtualClock())  # noqa: SLF001 - seek path
        seen["rebuilt_sinks"] = rebuilt.dispatcher.sinks
        seen["flushed_on_rebuild"] = speaker.flushes == flushes + 1
        rebuilt_call = Call(
            id="review-rebuilt",
            rule_id="test",
            priority=2,
            text="rebuilt",
            tags=[],
            deadline_ms=1000,
            lap=0,
            t=0.0,
            trigger_t=0.0,
        )
        hub.speak(rebuilt_call)
        rebuilt.dispatcher.metrics.note_handoff(rebuilt_call.id, handoff)
        assert speaker.on_spoken is not None
        speaker.on_spoken(rebuilt_call.id, handoff + 0.2)
        seen["rebuilt_tts_ms"] = rebuilt.dispatcher.metrics.summary()["tts_ms"]["p99"]
        seen["dropped_state"] = {row["id"]: row["audio"] for row in hub.recent_calls}[
            "review-dropped"
        ]

    monkeypatch.setattr(cli_module, "_serve", fake_serve)
    rec = _make_recording(tmp_path)
    args = ["replay", str(rec), "--seed-db", str(tmp_path / "s.sqlite"), "--speed", "1"]
    assert main([*args, "--serve"]) == 0
    assert seen["streams"] is True
    assert seen["on_audio"] is True
    assert speaker in seen["sinks"]  # type: ignore[operator]
    assert speaker in seen["rebuilt_sinks"]  # type: ignore[operator]
    assert seen["speaker_name"] == speaker.name
    assert seen["flushed_on_rebuild"] is True
    assert seen["first_tts_ms"] == pytest.approx(100.0)
    assert seen["rebuilt_tts_ms"] == pytest.approx(200.0)
    assert seen["dropped_state"] == "dropped"
    assert speaker.closed
