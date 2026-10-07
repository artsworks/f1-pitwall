from __future__ import annotations

import re

from fastapi.testclient import TestClient

from pitwall.cli import main
from pitwall.config.loader import ConfigStore, config_hash
from pitwall.debrief import (
    _compound,
    _fit_points,
    _hindsight,
    _incidents_section,
    _pit_laps,
    _sc_runs,
    render_debrief,
    rules_version,
)
from pitwall.metrics import Metrics
from pitwall.model.deg import DegFit
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database


def _insert_lap(
    db: Database,
    uid: int,
    lap_num: int,
    *,
    sc_status: int = 0,
    invalid_reasons: tuple[str, ...] = (),
    valid: bool = True,
    tyre_age_laps: int = 0,
    fuel_kg: float = 5.0,
    lap_time_ms: int = 90_000,
) -> None:
    db.insert_lap(
        uid,
        0,
        LapSummary(
            lap_num,
            lap_time_ms,
            30_000,
            30_000,
            19,
            tyre_age_laps,
            3.0,
            valid,
            list(invalid_reasons),
            fuel_kg=fuel_kg,
            sc_status=sc_status,
        ),
    )


def test_debrief_preserves_track_zero_and_its_learning() -> None:
    db = Database(":memory:")
    db.upsert_session(1, track_id=0, session_type=0)
    db.set_param(0, 17, "deg_ms_per_lap", 123.4, 5)
    report = render_debrief(db, 1, ConfigStore().current())
    summary = report.split("<section id='summary'", 1)[1].split("</section>", 1)[0]
    assert summary.count("<td>0</td>") == 2
    assert "mindset unknown" in report
    assert "No pit stops." in report
    assert "123.4" in report


def test_debrief_empty_hindsight() -> None:
    assert _hindsight(None) == "—"
    assert _hindsight([]) == "—"


def test_debrief_marks_press_grades() -> None:
    db = Database(":memory:")
    uid = 141
    db.upsert_session(uid, track_id=7, session_type=15)
    db.insert_call(
        uid,
        {
            "outcome": "fired",
            "call_id": "press-call",
            "rule_id": "box_now",
            "lap": 2,
            "text": "Box now",
        },
    )
    db.grade_call(uid, "press-call", "box_now", "good", source="press")

    report = render_debrief(db, uid, ConfigStore().current())

    assert "<span class='chip'>press</span>" in report


def test_debrief_joins_calls_hindsight_grades_and_escapes_inputs(tmp_path) -> None:
    db = Database(tmp_path / "session.sqlite")
    db.upsert_session(140, track_id=7, session_type=15, started_at=1.0, config_hash="old")
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
            "text": "Box this lap",
            "inputs": {"fuel": -0.5},
            "suppressed_by": "cooldown",
        },
    )
    db.insert_call(
        140,
        {
            "outcome": "suppressed",
            "call_id": "c3",
            "rule_id": "unsafe_text",
            "lap": 2,
            "text": "<script>alert('x')</script>",
            "suppressed_by": "<b>reason</b>",
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
            },
            {
                "call_id": "c1",
                "rule_id": "box_now",
                "lap": 2,
                "metric": "<b>kind</b>",
                "label": "<img>",
            },
        ],
    )
    db.insert_pit_event(140, 0, 2, 5000, 0, 30000, 90000, 92000, 91000)
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
    assert "Box this lap" in result
    assert "<td>suppressed · cooldown</td>" in result
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in result
    assert "<td>suppressed · &lt;b&gt;reason&lt;/b&gt;</td>" in result
    assert "profile unknown" in result and "rules unknown" in result
    assert "stop_cost_s: wrong · &lt;b&gt;kind&lt;/b&gt;: &lt;img&gt;" in result
    assert "cooldown" in result and "noise" in result
    assert "&lt;script&gt;" in result and "<script>alert(1)</script>" not in result
    assert "<th scope='col'>Pit lap</th>" in result
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


def test_compound_visual_and_actual_id_fallbacks() -> None:
    assert _compound(0, 19) == ("unk", "C2")
    assert _compound(0, 16) == ("unk", "C5")
    assert _compound(17, 16) == ("med", "MEDIUM")
    assert _compound(0, 7) == ("inter", "INTER")


def test_sc_runs_split_by_status_and_lap_gaps() -> None:
    db = Database(":memory:")
    db.upsert_session(151, track_id=16, session_type=15)
    for lap_num, status in enumerate([3, 0, 1, 1, 2, 2, 0], start=1):
        _insert_lap(db, 151, lap_num, sc_status=status)

    assert _sc_runs(db.laps_for(151)) == [(3, 4, "SC"), (5, 6, "VSC")]
    db.close()


def test_incidents_keep_unknown_sc_status_values() -> None:
    db = Database(":memory:")
    db.upsert_session(156, track_id=16, session_type=15)
    _insert_lap(db, 156, 1, sc_status=4)

    assert "<td>4</td>" in _incidents_section(db.laps_for(156))
    db.close()


def test_pit_laps_require_race_boundary_evidence_but_keep_explicit_events() -> None:
    db = Database(":memory:")

    def add_session(uid: int, session_type: int, values: list[tuple[int, int, tuple[str, ...]]]):
        db.upsert_session(uid, track_id=16, session_type=session_type)
        for lap_num, tyre_age, invalid_reasons in values:
            _insert_lap(
                db,
                uid,
                lap_num,
                invalid_reasons=invalid_reasons,
                tyre_age_laps=tyre_age,
            )
        return db.laps_for(uid)

    in_lap = add_session(152, 15, [(8, 8, ()), (9, 0, ("pitted",))])
    out_lap = add_session(153, 15, [(8, 8, ("pitted",)), (9, 0, ())])
    bare_reset = add_session(154, 15, [(8, 8, ()), (9, 0, ())])
    no_stop = add_session(155, 15, [(8, 8, ()), (9, 9, ())])
    practice = add_session(156, 5, [(8, 8, ()), (9, 0, ("pitted",))])

    assert _pit_laps([], in_lap, 15) == [9]
    assert _pit_laps([], out_lap, 15) == [8]
    assert _pit_laps([], bare_reset, 15) == [8]
    assert _pit_laps([], no_stop, 15) == []
    assert _pit_laps([], practice, 5) == []

    db.insert_pit_event(156, 0, 15, 5_000, 0, 30_000, 90_000, 92_000, 91_000)
    assert _pit_laps(db.pit_events_for_session(156), practice, 5) == [15]
    db.close()


def test_fit_points_use_only_usable_laps_and_adjust_for_tyre_age() -> None:
    db = Database(":memory:")
    uid = 155
    db.upsert_session(uid, track_id=16, session_type=15)
    _insert_lap(db, uid, 11)
    _insert_lap(db, uid, 12, tyre_age_laps=5)
    _insert_lap(db, uid, 13, tyre_age_laps=6)
    _insert_lap(db, uid, 14, valid=False, invalid_reasons=("spin",), tyre_age_laps=7)
    _insert_lap(db, uid, 15, sc_status=1, tyre_age_laps=8)
    _insert_lap(db, uid, 16, lap_time_ms=0, tyre_age_laps=9)
    fit = DegFit(90_000, 100.0, 0.0, 2, 50.0, 1.0, "fit")
    db.upsert_stint(uid, 0, 19, 12, 16, fit)

    points = _fit_points(db.stints_for_session(uid)[0], db.laps_for(uid))

    assert points == [(12, 90_500.0), (13, 90_600.0)]
    db.close()


def test_debrief_layout_hooks() -> None:
    db = Database(":memory:")
    uid = 16
    settings = ConfigStore().current()
    recording_path = "/tmp/<b>x</b>.f1bin.zst"
    db.upsert_session(
        uid,
        track_id=16,
        session_type=15,
        started_at=1_700_000_000,
        config_hash=config_hash(settings),
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
                ers_deployed_j=5_189_100 if lap_num == 1 else 0.0,
                sc_status={3: 1, 4: 2, 5: 3}.get(lap_num, 0),
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
    report = render_debrief(db, uid, settings)
    assert "class='agenda'" in report
    assert "<span class='n'>01</span>" in report
    assert "href='#pace'" in report
    assert report.count("badge det") >= 8
    assert "&lt;b&gt;x&lt;/b&gt;.f1bin.zst" in report
    assert "<b>x</b>" not in report
    assert "profile lite" in report
    assert f"config {config_hash(settings)}" in report
    assert f"rules {rules_version(settings)}" in report
    assert "mindset aggressive" in report
    assert "<td>—<details class='why'>" in report
    assert "BRAZIL · RACE · 6 LAPS" in report
    assert "3 clean green laps · 1 stop" in report
    assert "No pit events stored. Stint change on L3." in report
    assert "<th scope='col'>Track name</th>" in report
    assert "<td>Brazil</td>" in report
    strategy_section = report.split("<section id='strategy'", 1)[1].split("</section>", 1)[0]
    assert "<td>[]</td>" not in strategy_section
    assert "class='pt hard'" in report
    assert "class='pt med'" in report
    assert "class='pt inv'" in report
    for rule in (".pt.unk", ".fit.unk", ".stint rect.unk"):
        assert rule in report
    assert "class='fit hard'" in report
    assert re.search(r"<polyline class='fit hard' points='", report)
    fit_rule = re.search(r"\.fit \{([^}]*)\}", report)
    assert fit_rule and "fill:none" in fit_rule.group(1)
    assert "age 4, VSC</title>" in report
    assert "class='band'" in report
    assert "class='pitline'" in report
    axis_rule = re.search(r"\.axis \{([^}]*)\}", report)
    assert axis_rule and "fill:none" in axis_rule.group(1)
    assert ".stint .lbl { fill:var(--bg); }" in report
    assert "class='row cols-2 stint-row'" in report
    incidents_section = report.split("<section id='incidents'", 1)[1].split("</section>", 1)[0]
    assert "ERS deployed (MJ)" in incidents_section
    assert "<td>5.2</td>" in incidents_section
    for status in ("green", "SC", "VSC", "formation"):
        assert f"<td>{status}</td>" in incidents_section
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
