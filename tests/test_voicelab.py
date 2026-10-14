from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from pitwall.config.loader import ConfigStore
from pitwall.metrics import Metrics
from pitwall.server.app import create_app
from pitwall.server.hub import Hub


def _app(url: str, tmp_path: Path) -> TestClient:
    store = ConfigStore(
        overrides={"speech": {"omnivoice_url": url, "voices_dir": str(tmp_path)}},
        isolated=True,
    )
    return TestClient(create_app(Hub(), store, Metrics()))


def _speech_posts(state) -> int:  # type: ignore[no-untyped-def]
    return sum(1 for path, _ in state.requests if path == "/v1/audio/speech")


def test_render_combos_and_caching(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    client = _app(url, tmp_path)

    resp = client.post(
        "/api/voicelab/render",
        json={"stems": ["Box this lap", "Gap ahead"], "slots": ["P4", "Verstappen"]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == url + "/v1/audio/speech"
    assert len(data["combos"]) == 4
    for combo in data["combos"]:
        assert combo["id"] == f"{combo['stem']} | {combo['slot']}"
        for name in ("stem", "slot", "spliced", "full"):
            assert base64.b64decode(combo["wavs"][name])[:4] == b"RIFF"
        assert set(combo["ms"]) == {"stem", "slot", "full", "splice"}
    # 2 stems + 2 slots + 4 full texts, each rendered once.
    assert _speech_posts(state) == 8


def test_render_url_override(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    client = _app("http://127.0.0.1:9", tmp_path)

    resp = client.post(
        "/api/voicelab/render",
        json={"stems": ["Box"], "slots": ["P4"], "url": url},
    )

    assert resp.status_code == 200
    assert len(resp.json()["combos"]) == 1


def test_render_upstream_error_returns_502(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    state.speech_status = 500
    client = _app(url, tmp_path)

    resp = client.post(
        "/api/voicelab/render",
        json={"stems": ["Box"], "slots": ["P4"]},
    )

    assert resp.status_code == 502
    assert resp.json()["error"]


def test_render_multi_lap_chain(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    client = _app(url, tmp_path)
    segments = client.get("/api/voicelab/vocab").json()["chains"]["lap_time"][0]

    resp = client.post("/api/voicelab/render_multi", json={"segments": segments})

    assert resp.status_code == 200
    data = resp.json()
    assert data["full_text"] == " ".join(segments)
    assert len(data["segments"]) == len(segments)
    assert base64.b64decode(data["spliced"])[:4] == b"RIFF"
    assert base64.b64decode(data["full"])[:4] == b"RIFF"
    assert _speech_posts(state) == len(segments) + 1


def test_grade_appends_jsonl(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    client = _app(url, tmp_path)

    for combo in ("a | b", "c | d"):
        resp = client.post(
            "/api/voicelab/grade",
            json={"combo": combo, "variant": "spliced", "score": 4, "note": "ok"},
        )
        assert resp.status_code == 200

    path = tmp_path / ".voicelab-grades.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["combo"] == "a | b"
    assert first["score"] == 4
    assert first["t"] > 0


def test_grade_score_out_of_range(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    client = _app(url, tmp_path)

    resp = client.post(
        "/api/voicelab/grade",
        json={"combo": "a | b", "variant": "full", "score": 6},
    )

    assert resp.status_code == 422


def test_voicelab_page_and_vocab(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    client = _app(url, tmp_path)

    page = client.get("/voicelab")
    assert page.status_code == 200
    assert "voicelab" in page.text

    vocab = client.get("/api/voicelab/vocab").json()
    assert set(vocab["slots"]) == {
        "drivers",
        "teams",
        "positions",
        "temps",
        "lap_time_atoms",
        "digits",
    }
    assert vocab["stems"]
    assert vocab["chains"]["lap_time"][0] == [
        "one minute",
        "nineteen",
        "point",
        "two",
        "seconds",
    ]
