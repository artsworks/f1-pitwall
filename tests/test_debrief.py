from __future__ import annotations

from fastapi.testclient import TestClient

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.debrief import render_debrief
from pitwall.metrics import Metrics
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database


def test_debrief_joins_calls_hindsight_grades_and_escapes_inputs(tmp_path) -> None:
    db = Database(tmp_path / "session.sqlite")
    db.upsert_session(140, track_id=7, session_type=15, started_at=1.0)
    db.insert_lap(
        140,
        0,
        LapSummary(2, 91_100, 30_000, 31_000, 7, 2, 6.0, True, []),
    )
    db.insert_call(
        140,
        {
            "outcome": "fired",
            "call_id": "c1",
            "rule_id": "box_now",
            "lap": 2,
            "text": "Box now",
            "inputs": {"gap": 2.4, "user": "<script>alert(1)</script>"},
        },
    )
    db.insert_call(
        140,
        {
            "outcome": "suppressed",
            "call_id": "c2",
            "rule_id": "fuel_short",
            "lap": 2,
            "inputs": {"fuel": -0.5},
            "suppressed_by": "cooldown",
        },
    )
    db.replace_outcomes(
        140,
        [
            {
                "call_id": "c1",
                "rule_id": "box_now",
                "lap": 2,
                "metric": "stop_cost_s",
                "label": "wrong",
                "error": 3.0,
                "detail": "Pit loss exceeded plan",
            }
        ],
    )
    db.grade_call(140, "c1", "box_now", "noise")
    settings = ConfigStore().current()

    result = render_debrief(db, 140, settings)
    assert all(
        f"id='{section}'" in result
        for section in (
            "summary",
            "pace",
            "sectors",
            "tyres",
            "strategy",
            "radio",
            "incidents",
            "actions",
        )
    )
    assert "Box now" in result and "Pit loss exceeded plan" in result
    assert "cooldown" in result and "noise" in result
    assert "&lt;script&gt;" in result and "<script>alert(1)</script>" not in result
    assert (
        main(
            [
                "debrief",
                "--db",
                str(tmp_path / "session.sqlite"),
                "--session",
                "140",
                "--out",
                str(tmp_path / "debrief.html"),
            ]
        )
        == 0
    )
    assert (tmp_path / "debrief.html").read_text() == result

    client = TestClient(create_app(Hub(), ConfigStore(), Metrics(), db=db))
    assert client.get("/debrief/140").status_code == 200
    assert "data-grade='good'" in client.get("/debrief/140").text
    assert "data-grade='good'" not in result
    assert client.get("/debrief/999").status_code == 404
    assert (
        client.post(
            "/api/debrief/140/grade",
            json={
                "call_id": "c1",
                "grade": "good",
            },
        ).status_code
        == 200
    )
    assert db.grades_for_session(140)[0]["grade"] == "good"
    assert client.post("/api/debrief/140/grade", json=[]).status_code == 400
    assert (
        client.post(
            "/api/debrief/140/grade",
            json={
                "call_id": "c1",
                "grade": "invalid",
            },
        ).status_code
        == 400
    )
    db.close()
