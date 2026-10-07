from __future__ import annotations

from pitwall.digest import build_digest
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
