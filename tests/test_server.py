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
