from __future__ import annotations

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from pitwall.config.loader import ConfigStore
from pitwall.metrics import Metrics
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.server.pin import PIN_COOKIE, PinGate, forwarded


def test_pin_is_four_digits() -> None:
    gate = PinGate()
    assert len(gate.pin) == 4
    assert gate.pin.isdigit()


def test_allowed_hosts_and_cookie() -> None:
    gate = PinGate(pin="1234")
    assert gate.allowed("127.0.0.1", None)
    assert gate.allowed("::1", None)
    assert not gate.allowed("testclient", None)
    assert not gate.allowed("192.168.1.5", None)
    assert gate.allowed("testclient", gate.token)


def test_loopback_is_not_trusted_when_proxied_or_disabled() -> None:
    gate = PinGate(pin="1234")
    assert not gate.allowed("127.0.0.1", None, proxied=True)
    assert gate.allowed("127.0.0.1", gate.token, proxied=True)
    strict = PinGate(pin="1234", trust_local=False)
    assert not strict.allowed("127.0.0.1", None)
    assert strict.allowed("127.0.0.1", strict.token)


def test_forwarded_detects_proxy_headers() -> None:
    assert not forwarded({"host": "localhost:8000"})
    assert forwarded({"x-forwarded-for": "203.0.113.9"})
    assert forwarded({"forwarded": "for=203.0.113.9"})


def test_right_pin_stays_locked_until_timeout() -> None:
    now = [0.0]
    gate = PinGate(pin="1234", clock=lambda: now[0])
    for _ in range(5):
        gate.check("192.168.1.5", "0000")
    locked = gate.check("192.168.1.5", "1234")
    assert not locked.ok
    assert locked.retry_after_s > 0

    now[0] = 60.0
    assert gate.check("192.168.1.5", "1234").ok


def test_success_resets_wrong_pin_count() -> None:
    gate = PinGate(pin="1234")
    for _ in range(4):
        result = gate.check("192.168.1.5", "0000")
    assert result.tries_left == 1
    assert gate.check("192.168.1.5", "1234").ok
    for _ in range(4):
        result = gate.check("192.168.1.5", "0000")
    assert result.tries_left == 1
    assert result.retry_after_s == 0.0


def test_lockout_is_per_host() -> None:
    gate = PinGate(pin="1234")
    for _ in range(5):
        gate.check("192.168.1.5", "0000")
    assert gate.check("192.168.1.5", "1234").retry_after_s > 0
    assert gate.check("192.168.1.6", "1234").ok


def _client(gate: PinGate | None = None) -> TestClient:
    return TestClient(create_app(Hub(), ConfigStore(), Metrics(), pin_gate=gate))


def test_pin_middleware_protects_http_but_allows_pin_and_static() -> None:
    client = _client(PinGate(pin="1234"))
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/pin?next=%2F"
    assert client.get("/api/config").status_code == 401
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/pin").status_code == 200


def test_pin_protects_websocket_until_cookie_is_set() -> None:
    client = _client(PinGate(pin="1234"))
    with client.websocket_connect("/ws") as websocket:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 4003

    wrong = client.post("/api/pin", json={"pin": "0000"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "wrong", "tries_left": 4}

    right = client.post("/api/pin", json={"pin": "1234"})
    assert right.status_code == 200
    assert right.json() == {"ok": True}
    assert right.cookies.get(PIN_COOKIE)
    assert client.cookies.get(PIN_COOKIE)
    assert client.get("/").status_code == 200
    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_json()["type"] == "hello"


def test_pin_api_returns_lockout_after_five_wrong_tries() -> None:
    client = _client(PinGate(pin="1234"))
    for _ in range(5):
        client.post("/api/pin", json={"pin": "0000"})
    locked = client.post("/api/pin", json={"pin": "1234"})
    assert locked.status_code == 429
    assert locked.json()["error"] == "locked"
    assert locked.json()["retry_after_s"] > 0


def test_loopback_proxy_request_needs_pin() -> None:
    app = create_app(Hub(), ConfigStore(), Metrics(), pin_gate=PinGate(pin="1234"))
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/api/config").status_code == 200
    proxied = {"Forwarded": "for=203.0.113.9"}
    assert client.get("/api/config", headers=proxied).status_code == 401
    with client.websocket_connect("/ws", headers=proxied) as websocket:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 4003


def test_pin_cookie_is_secure_over_https() -> None:
    http = _client(PinGate(pin="1234")).post("/api/pin", json={"pin": "1234"})
    assert "secure" not in http.headers["set-cookie"].lower()
    app = create_app(Hub(), ConfigStore(), Metrics(), pin_gate=PinGate(pin="1234"))
    https = TestClient(app, base_url="https://testserver").post("/api/pin", json={"pin": "1234"})
    assert "secure" in https.headers["set-cookie"].lower()


def test_app_without_pin_gate_keeps_current_access() -> None:
    client = _client()
    assert client.get("/").status_code == 200
