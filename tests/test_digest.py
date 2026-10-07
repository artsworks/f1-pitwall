from __future__ import annotations

import json

from pitwall.cli import main
from pitwall.digest import build_digest, call_quality, format_digest
from pitwall.store.db import Database


def test_digest_separates_press_grades_and_keeps_bookmark_kind() -> None:
    db = Database(":memory:")
    uid = 73
    db.upsert_session(uid, track_id=2, session_type=15)
    db.insert_call(
        uid,
        {
            "outcome": "fired",
            "call_id": "press-call",
            "rule_id": "tyre_temp",
            "lap": 3,
            "text": "Tyres are hot",
        },
    )
    db.grade_call(uid, "press-call", "tyre_temp", "good", source="press")
    db.insert_call(
        uid,
        {"outcome": "bookmark", "t": 4.0, "lap": 3, "kind": "tap", "context": {"lap_num": 3}},
    )

    digest = build_digest(db, uid, {})

    assert digest["calls"]["tyre_temp"]["human_good"] == 0
    assert digest["calls"]["tyre_temp"]["press_good"] == 1
    assert digest["bookmarks"][0]["kind"] == "tap"
    assert "quality 100% good · neg 0% · 0 unanswered questions · 0 ungraded" in format_digest(
        digest
    )


def test_call_quality_counts_fired_grades_questions_and_exclusions() -> None:
    db = Database(":memory:")
    uid = 74
    db.upsert_session(uid, track_id=2, session_type=15)
    for call_id, rule_id in (
        ("call-1", "tyre_temp"),
        ("call-2", "pit_window"),
        ("reply-call", "reply"),
        ("menu-call", "menu:pit"),
    ):
        db.insert_call(
            uid,
            {
                "outcome": "fired",
                "call_id": call_id,
                "rule_id": rule_id,
                "text": "Call",
            },
        )
    db.insert_call(
        uid,
        {
            "outcome": "fired",
            "call_id": "repeat-call",
            "rule_id": "tyre_temp",
            "inputs": {"repeat_of": "call-1"},
            "text": "Call",
        },
    )
    db.grade_call(uid, "call-1", "tyre_temp", "good", source="press")
    db.grade_call(uid, "call-2", "pit_window", "noise")
    db.insert_call(
        uid,
        {
            "outcome": "neg",
            "call_id": "call-2",
            "rule_id": "pit_window",
            "text": "No",
        },
    )
    db.insert_call(
        uid,
        {
            "outcome": "driver_input",
            "item_id": "fight",
            "kind": "question",
            "inputs": {"case": "unknown"},
            "text": "No useful answer",
        },
    )
    db.insert_call(
        uid,
        {
            "outcome": "driver_input",
            "item_id": "pit",
            "kind": "question",
            "inputs": {"case": "soon"},
            "text": "Box lap 12",
        },
    )

    before = db.outcomes_for_session(uid)
    quality = call_quality(db, uid)

    assert quality == {
        "fired": 2,
        "graded": 2,
        "ungraded": 0,
        "good": 1,
        "good_pct": 50.0,
        "neg": 1,
        "neg_rate_pct": 50.0,
        "press_graded": 1,
        "questions": 2,
        "unanswered_questions": 1,
    }
    assert db.outcomes_for_session(uid) == before


def test_call_quality_uses_none_percentages_without_fired_calls() -> None:
    db = Database(":memory:")
    quality = call_quality(db, 75)

    assert quality["good_pct"] is None
    assert quality["neg_rate_pct"] is None


def test_format_digest_uses_na_without_fired_calls() -> None:
    db = Database(":memory:")
    db.upsert_session(76, track_id=2, session_type=15)

    digest = build_digest(db, 76, {})

    assert "quality n/a good · neg n/a · 0 unanswered questions · 0 ungraded" in format_digest(
        digest
    )


def test_stats_quality_cli_prints_sessions_and_trend(tmp_path, capsys) -> None:
    path = tmp_path / "quality.sqlite"
    db = Database(path)
    for uid, started_at, grade in ((81, 100.0, None), (82, 200.0, "good")):
        db.upsert_session(uid, track_id=2, session_type=15, started_at=started_at)
        db.insert_call(
            uid,
            {
                "outcome": "fired",
                "call_id": f"call-{uid}",
                "rule_id": "tyre_temp",
                "text": "Tyres are hot",
            },
        )
        if grade:
            db.grade_call(uid, f"call-{uid}", "tyre_temp", grade)
    db.close()

    assert main(["stats", "--quality", "--db", str(path)]) == 0
    output = capsys.readouterr().out
    assert "1 fired · good 0%" in output
    assert "1 fired · good 100%" in output
    assert "trend: good% 0.0 -> 100.0 (+100.0) over 2 sessions" in output


def test_stats_quality_cli_json_includes_trend(tmp_path, capsys) -> None:
    path = tmp_path / "quality-json.sqlite"
    db = Database(path)
    for uid, started_at in ((91, 100.0), (92, 200.0)):
        db.upsert_session(uid, track_id=2, session_type=15, started_at=started_at)
        db.insert_call(
            uid,
            {"outcome": "fired", "call_id": f"call-{uid}", "rule_id": "r", "text": "Call"},
        )
    db.close()

    assert main(["stats", "--quality", "--json", "--db", str(path), "--sessions", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [row["uid"] for row in report["sessions"]] == [91, 92]
    assert report["trend"]["sessions"] == 2
