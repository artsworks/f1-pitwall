from __future__ import annotations

from pitwall.evaluate import evaluate_corpus
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database


def test_evaluation_separates_modes_tracks_and_invalid_laps(tmp_path) -> None:
    db = Database(tmp_path / "sessions.sqlite")
    for uid, track, mode, lap_time, valid in (
        (11, 7, "on", 90_000, True),
        (12, 7, "off", 95_000, True),
        (13, 7, "off", 101_000, False),
        (14, 8, "on", 85_000, True),
    ):
        db.upsert_session(uid, track_id=track, session_type=15, started_at=float(uid))
        db.set_session_origin(
            uid, started_at=float(uid), recording_path=f"{uid}.f1bin", calls_mode=mode
        )
        db.insert_lap(
            uid,
            0,
            LapSummary(
                1, lap_time, 30_000, 30_000, 7, 2, 6.0, valid, [] if valid else ["off_track"]
            ),
        )
    db.insert_call(
        11,
        {
            "outcome": "fired",
            "call_id": "c1",
            "rule_id": "box_now",
            "lap": 1,
        },
    )
    db.grade_call(11, "c1", "box_now", "wrong")
    db.insert_lap(13, 0, LapSummary(2, 180_000, 60_000, 60_000, 7, 2, 5.0, False, ["red_flag"]))
    result = evaluate_corpus(db)
    assert result["tracks"][0]["comparable"]
    assert result["tracks"][0]["on"]["lap_time_s"]["p50"] == 90.0
    assert result["tracks"][0]["off"]["lap_time_s"]["p50"] == 95.0
    assert result["tracks"][0]["off"]["mistake_rate"] == 0.5
    assert result["tracks"][0]["off"]["invalid_laps"] == 2
    assert result["tracks"][0]["on"]["negative_call_grades"] == 1
    assert not result["tracks"][1]["comparable"]
    assert len(evaluate_corpus(db, 8)["tracks"]) == 1
    db.close()
