from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

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
    from pitwall.audio.piper_tts import PiperSpeaker
    from pitwall.cli import main

    class _Player:
        def play(self, wav: bytes) -> None:
            pass

        def stop(self) -> None:
            pass

    class _Speaker(PiperSpeaker):
        flushes = 0
        closed = False

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

        def close(self) -> None:
            self.closed = True
            super().close()

    speaker = _Speaker(lambda text: (text.encode(), 0.0), _Player())
    monkeypatch.setattr(speaker_module, "make_speaker", lambda _settings: speaker)
    seen: dict[str, object] = {}

    async def fake_serve(engine, hub, _store, coro, *, review) -> None:  # type: ignore[no-untyped-def]
        coro.close()
        seen["streams"] = hub.streams_audio
        seen["on_audio"] = speaker.on_audio == hub.audio
        seen["sinks"] = engine.dispatcher.sinks
        flushes = speaker.flushes
        rebuilt = review._engine_factory(VirtualClock())  # noqa: SLF001 - seek path
        seen["rebuilt_sinks"] = rebuilt.dispatcher.sinks
        seen["flushed_on_rebuild"] = speaker.flushes == flushes + 1

    monkeypatch.setattr(cli_module, "_serve", fake_serve)
    rec = _make_recording(tmp_path)
    args = ["replay", str(rec), "--seed-db", str(tmp_path / "s.sqlite"), "--speed", "1"]
    assert main([*args, "--serve"]) == 0
    assert seen["streams"] is True
    assert seen["on_audio"] is True
    assert speaker in seen["sinks"]  # type: ignore[operator]
    assert speaker in seen["rebuilt_sinks"]  # type: ignore[operator]
    assert seen["flushed_on_rebuild"] is True
    assert speaker.closed
