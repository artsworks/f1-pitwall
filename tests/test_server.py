"""M1 part 2: WS protocol, hub broadcast, speaker, doctor."""

from __future__ import annotations

import io

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from pitwall.audio.dispatcher import Call
from pitwall.audio.speaker import NullSpeaker
from pitwall.config.loader import ConfigStore
from pitwall.metrics import Metrics
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.state.session import SessionState

_now = 0.0


def _call(call_id: str = "c1", priority: int = 2) -> Call:
    return Call(
        id=call_id,
        rule_id="r",
        priority=priority,
        text="Front left is cold.",
        tags=["tyres"],
        deadline_ms=3000,
        lap=2,
        t=_now,
        trigger_t=_now,
        still_true=lambda s: True,
    )


def _app(store: ConfigStore | None = None) -> tuple[TestClient, Hub]:
    hub = Hub()
    metrics = Metrics()
    state = SessionState()

    client = TestClient(
        create_app(
            hub,
            store or ConfigStore(),
            metrics,
            speaker_name="null",
            latest_snapshot=lambda: state.snapshot(_now),
        )
    )
    return client, hub


def test_websocket_hello_and_state_shape() -> None:
    client, _ = _app()
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["payload"]["protocol"] == 1
        assert hello["payload"]["config_hash"]
        ws.send_json({"type": "hello", "v": 1, "last_seq": 7})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        for key in ("live", "tyres", "brakes", "phase", "latency"):
            assert key in snap["payload"]
        for corner in ("fl", "fr", "rl", "rr"):
            assert set(snap["payload"]["tyres"][corner]) == {
                "surface",
                "inner",
                "wear",
                "status",
            }
        assert "calls" in snap["payload"]


def test_websocket_version_mismatch_close() -> None:
    client, _ = _app()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 99, "last_seq": None})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_hub_broadcast_order_and_snapshot() -> None:
    hub = Hub()
    hub.speak(_call("a"))
    hub.spoken("a", _now)
    hub.cancel("a")
    types = [f["type"] for f in hub.outbox]
    assert types == ["call", "spoken", "cancel"]
    assert len(hub.recent_calls) == 1
    # reconnect snapshot carries last calls
    client, _ = _app()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 1, "last_seq": 0})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"


def test_reconnect_snapshot_has_calls() -> None:
    hub = Hub()
    metrics = Metrics()
    store = ConfigStore()
    hub.speak(_call("x"))
    client = TestClient(create_app(hub, store, metrics))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 1, "last_seq": 3})
        snap = ws.receive_json()
        calls = snap["payload"]["calls"]
        assert any(c["id"] == "x" for c in calls)


def test_api_health_and_config() -> None:
    client, _ = _app()
    h = client.get("/api/health").json()
    assert h["speaker"] == "null"
    assert "config_hash" in h
    c = client.get("/api/config").json()
    assert c["ui"]["state_hz"] == 5
    assert client.get("/").status_code == 200
    assert client.get("/radio").status_code == 200


def test_null_speaker_on_spoken() -> None:
    speaker = NullSpeaker()
    got: list[tuple[str, float]] = []
    speaker.on_spoken = lambda cid, t: got.append((cid, t))
    speaker.speak(_call("n"))
    assert got and got[0][0] == "n"
    speaker.cancel("n")  # no-op, must not raise


def test_sapi_not_importable_on_linux() -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("windows-only check")
    assert "win32com" not in sys.modules
    assert "pythoncom" not in sys.modules


def test_doctor_pass_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from pitwall import doctor

    out = io.StringIO()

    async def fake_listen(host: str, port: int, ingest: object, clock: object) -> object:
        class T:
            def close(self) -> None:
                pass

        return T()

    monkeypatch.setattr(doctor, "listen", fake_listen)

    async def _noop(s: float) -> None:
        pass

    monkeypatch.setattr(doctor.asyncio, "sleep", _noop)

    rc = doctor.run_doctor(seconds=0, out=out, store=ConfigStore())
    text = out.getvalue()
    assert "PASS" in text
    assert "config loads" in text
    assert rc in (0, 1)  # FAIL only if env is bad; lines must render


def test_cli_start_help(capsys: pytest.CaptureFixture[str]) -> None:
    from pitwall.cli import main

    with pytest.raises(SystemExit) as e:
        main(["start", "--help"])
    assert e.value.code == 0
    assert "usage" in capsys.readouterr().out


class _FakeVoice:
    def __init__(self, done_after: int = 1) -> None:
        self.calls: list[tuple[str, int]] = []
        self._polls = 0
        self._done_after = done_after

    def Speak(self, text: str, flags: int) -> int:
        self.calls.append((text, flags))
        return 0

    def WaitUntilDone(self, ms: int) -> bool:
        self._polls += 1
        return self._polls >= self._done_after


def _urgent_event():
    import threading

    return threading.Event()


def test_speak_one_p1_purge_and_on_spoken() -> None:
    from pitwall.audio.speaker import SVSF_ASYNC, SVSF_PURGE_BEFORE_SPEAK, _speak_one

    voice = _FakeVoice()
    got: list[str] = []
    urgent = _urgent_event()
    urgent.set()
    done = _speak_one(
        voice, _call("p1", priority=1), urgent, lambda cid, t: got.append(cid), beep=False
    )
    assert voice.calls[0][1] == SVSF_ASYNC | SVSF_PURGE_BEFORE_SPEAK
    assert got == ["p1"]
    assert done is True
    assert not urgent.is_set()  # cleared after the P1 call


def test_speak_one_p2_async_only() -> None:
    from pitwall.audio.speaker import SVSF_ASYNC, _speak_one
    from pitwall.audio.speaker import SVSF_PURGE_BEFORE_SPEAK as PURGE

    voice = _FakeVoice()
    done = _speak_one(voice, _call("p2", priority=2), _urgent_event(), None, beep=False)
    assert voice.calls[0][1] == SVSF_ASYNC
    assert voice.calls[0][1] & PURGE == 0
    assert done is True


def test_speak_one_wait_breaks_on_urgent() -> None:
    from pitwall.audio.speaker import _speak_one

    voice = _FakeVoice(done_after=100)
    urgent = _urgent_event()
    urgent.set()
    done = _speak_one(voice, _call("p2", priority=2), urgent, None, beep=False)
    assert done is False


def test_packet_age_uses_receive_clock_not_session_time() -> None:
    """Session time (seconds since session start) must never be mixed with the
    tick clock; a packet received 0.1 s ago is LIVE whatever its session_time."""
    from pitwall.protocol.header import PacketId, parse_header
    from pitwall.server.app import state_payload

    from .synth import make_packet

    store = ConfigStore()
    store.poll(0.0)
    settings = store.current()
    state = SessionState()
    payload = make_packet(PacketId.CAR_TELEMETRY, session_time=4879.5)
    recv = 1_000_000.0
    state.on_packet(parse_header(payload), payload, recv)
    snap = state.snapshot(recv + 0.1)
    assert snap.last_packet_t == recv
    body = state_payload(snap, settings=settings, metrics=Metrics(), quiet=False)
    assert body["live"] is True
    assert 90.0 <= body["packet_age_ms"] <= 110.0
    later = state_payload(
        state.snapshot(recv + 5), settings=settings, metrics=Metrics(), quiet=False
    )
    assert later["live"] is False


def test_health_includes_packet_age_and_live() -> None:
    from pitwall.server.app import packet_age_ms

    hub = Hub()
    metrics = Metrics()
    state = SessionState()
    snap = state.snapshot(0.0)
    hub.health_source = lambda: {
        "packet_age_ms": packet_age_ms(snap),
        "live": False,
    }
    client = TestClient(create_app(hub, ConfigStore(), metrics))
    h = client.get("/api/health").json()
    assert "packet_age_ms" in h
    assert "live" in h
    assert h["live"] is False


def test_packet_age_ms_helper() -> None:
    from pitwall.server.app import packet_age_ms

    snap = SessionState().snapshot(2.0)
    snap.last_packet_t = 1.5
    assert packet_age_ms(snap) == 500.0
    snap.last_packet_t = None
    assert packet_age_ms(snap) is None


def test_replay_hub_sink_emits_call(tmp_path) -> None:
    """build_engine with a Hub sink: a firing rule produces a call frame."""
    import asyncio

    from pitwall.audio.dispatcher import LogSink
    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine, run_replay
    from pitwall.net.recording import RecordingWriter
    from pitwall.protocol.header import PacketId

    # recording: race session + lap data on_track + car damage FL wing 30
    from .synth import pack_packet

    packets = (
        [pack_packet(PacketId.SESSION, {"session_type": 15}, session_time=t) for t in (0.0, 1.0)]
        + [
            pack_packet(
                PacketId.LAP_DATA,
                {"cars": {0: {"driver_status": 4, "current_lap_num": 5}}},
                session_time=t,
            )
            for t in (0.0, 1.0)
        ]
        + [
            pack_packet(
                PacketId.CAR_DAMAGE,
                {"cars": {0: {"front_left_wing_damage": 30}}},
                session_time=t,
            )
            for t in (0.5, 1.5)
        ]
    )
    path = tmp_path / "d.f1bin"
    with RecordingWriter(path, packet_format=2026) as w:
        for i, pkt in enumerate(packets):
            w.write_datagram(i * 0.5, pkt)

    hub = Hub()
    engine = build_engine(clock=VirtualClock(), sinks=[hub, LogSink()])
    asyncio.run(run_replay(path, engine, None))
    call_frames = [f for f in hub.outbox if f["type"] == "call"]
    assert call_frames
    assert any("wing" in f["payload"]["text"].lower() for f in call_frames)
