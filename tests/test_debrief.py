from __future__ import annotations

import re

from fastapi.testclient import TestClient

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.debrief import render_debrief, rules_version
from pitwall.metrics import Metrics
from pitwall.model.deg import DegFit
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database


def test_debrief_preserves_track_zero_and_its_learning() -> None:
    db = Database(":memory:")
    db.upsert_session(1, track_id=0, session_type=15)
    db.set_param(0, 17, "deg_ms_per_lap", 123.4, 5)
    report = render_debrief(db, 1, ConfigStore().current())
    assert "<td>0</td>" in report
    assert "123.4" in report


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
    latest = client.get("/debrief/latest", follow_redirects=False)
    assert latest.status_code == 307 and latest.headers["location"] == "/debrief/140"
    index = client.get("/debrief")
    assert index.status_code == 200 and "href='/debrief/140'" in index.text
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


def test_debrief_latest_404_and_empty_index() -> None:
    client = TestClient(create_app(Hub(), ConfigStore(), Metrics(), db=Database(":memory:")))
    assert client.get("/debrief/latest", follow_redirects=False).status_code == 404
    assert "No sessions stored yet" in client.get("/debrief").text


def test_debrief_layout_hooks() -> None:
    db = Database(":memory:")
    uid = 16
    recording_path = "/tmp/<b>x</b>.f1bin.zst"
    db.upsert_session(
        uid,
        track_id=16,
        session_type=15,
        started_at=1_700_000_000,
        config_hash="abc123",
        recording_path=recording_path,
        calls_mode="enabled",
    )
    for lap_num in range(1, 7):
        visual = 18 if lap_num <= 3 else 17
        compound = 19 if lap_num <= 3 else 18
        db.insert_lap(
            uid,
            0,
            LapSummary(
                lap_num,
                100_000 + lap_num * 100,
                30_000,
                30_000,
                compound,
                lap_num,
                3.0,
                lap_num not in (3, 5),
                ["pitted"] if lap_num == 3 else [],
                fuel_kg=5.0,
                sc_status=1 if lap_num == 5 else 0,
                visual=visual,
            ),
        )
    fit = DegFit(
        base_ms=100_000,
        deg_ms_per_lap=100.0,
        fuel_ms_per_lap=0.0,
        n=2,
        rmse_ms=50.0,
        confidence=1.0,
        source="fit",
    )
    db.upsert_stint(uid, 0, 19, 1, 3, fit)
    db.upsert_stint(uid, 0, 18, 4, 6, fit)
    db.insert_call(
        uid,
        {
            "t": 1,
            "outcome": "fired",
            "call_id": "fired-1",
            "rule_id": "pit_window",
            "priority": 1,
            "lap": 3,
            "text": "Box now",
            "mindset": "aggressive",
            "inputs": {"gap": 2.4},
        },
    )
    db.insert_call(
        uid,
        {
            "t": 2,
            "outcome": "suppressed",
            "call_id": "suppressed-1",
            "rule_id": "fuel_short",
            "priority": 2,
            "lap": 5,
            "suppressed_by": "cooldown",
        },
    )
    db.grade_call(uid, "fired-1", "pit_window", "wrong")
    settings = ConfigStore().current()

    report = render_debrief(db, uid, settings)
    assert "class='agenda'" in report
    assert "<span class='n'>01</span>" in report
    assert "href='#pace'" in report
    assert report.count("badge det") >= 8
    assert "&lt;b&gt;x&lt;/b&gt;.f1bin.zst" in report
    assert "<b>x</b>" not in report
    assert "profile lite" in report
    assert "config abc123" in report
    assert f"rules {rules_version(settings)}" in report
    assert "mindset aggressive" in report
    assert "BRAZIL · RACE · 6 LAPS" in report
    assert "class='pt hard'" in report
    assert "class='pt med'" in report
    assert "class='pt inv'" in report
    assert "class='fit hard'" in report
    assert "class='band'" in report
    assert "class='pitline'" in report
    lap_y = {
        int(lap_num): float(y)
        for y, lap_num in re.findall(
            r"<circle class='pt [^']+' cx='[^']+' cy='([^']+)' r='[^']+'><title>Lap (\d+):",
            report,
        )
    }
    assert lap_y[6] < lap_y[1]
    assert "PIT L3" in report
    assert "class='chip med'" in report
    assert "IntersectionObserver" in report
    assert "@media print" in report
    assert "<link" not in report
    assert "src=" not in report

    editable = render_debrief(db, uid, settings, editable=True)
    assert "class='on bad' data-call='fired-1' data-grade='wrong'" in editable
    assert "tr class='sup'" in editable
    db.close()
