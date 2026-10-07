from __future__ import annotations

from pitwall.config.loader import ConfigStore
from pitwall.config.models import MenuItemModel, MenuSettings, RuleDefModel
from pitwall.propose import propose_thresholds
from pitwall.questions import question_candidates
from pitwall.rules.expr import expr_names
from pitwall.store.db import Database


def _rule() -> RuleDefModel:
    return RuleDefModel(id="test_rule", priority=1, when="th.limit < lap_num", say="Test")


def _menu() -> MenuSettings:
    return MenuSettings(
        items=[
            MenuItemModel(
                id="tyres",
                label="Tyres gone?",
                related_rules=["test_rule"],
            )
        ]
    )


def _ask(db: Database, uid: int, lap: int, t: float, value: float, item_id: str = "tyres") -> None:
    db.insert_call(
        uid,
        {
            "outcome": "driver_input",
            "t": t,
            "lap": lap,
            "item_id": item_id,
            "kind": "question",
            "inputs": {"signals": {"lap_num": value}},
        },
    )


def test_expr_names_excludes_thresholds_and_helpers() -> None:
    assert expr_names("th.limit < lap_num and abs(gap_ahead_s) > 1 and fresh('x')") == {
        "lap_num",
        "gap_ahead_s",
    }


def test_recurring_uncovered_questions_group_sessions_and_median_signals() -> None:
    db = Database(":memory:")
    for uid, start, values in ((1, 1.0, (1.0, 3.0)), (2, 2.0, (5.0, 7.0))):
        db.upsert_session(uid, started_at=start)
        _ask(db, uid, 5, 10.0, values[0])
        _ask(db, uid, 6, 20.0, values[1])

    candidates = question_candidates(db, _menu(), [_rule()], thresholds={"limit": 4.0})

    assert candidates == [
        {
            "item_id": "tyres",
            "label": "Tyres gone?",
            "sessions": 2,
            "asks": 4,
            "uncovered": 4,
            "window_laps": 2,
            "at_ask": {"lap_num": 4.0},
            "rules": [{"rule_id": "test_rule", "thresholds": {"limit": 4.0}}],
            "suggestion": "fire earlier: review these thresholds",
        }
    ]


def test_covered_questions_do_not_meet_recurrence_threshold() -> None:
    db = Database(":memory:")
    db.upsert_session(1)
    db.insert_call(
        1,
        {
            "outcome": "fired",
            "call_id": "c1",
            "rule_id": "test_rule",
            "t": 9.0,
            "lap": 4,
        },
    )
    _ask(db, 1, 5, 10.0, 5.0)
    _ask(db, 1, 8, 20.0, 7.0)

    assert question_candidates(db, _menu(), [_rule()], thresholds={"limit": 4.0}) == []


def test_repeat_calls_do_not_cover_questions() -> None:
    db = Database(":memory:")
    db.upsert_session(1)
    db.insert_call(
        1,
        {
            "outcome": "fired",
            "call_id": "repeat-call",
            "rule_id": "test_rule",
            "t": 9.0,
            "lap": 4,
            "inputs": {"repeat_of": "original-call"},
        },
    )
    _ask(db, 1, 5, 10.0, 5.0)
    _ask(db, 1, 6, 20.0, 7.0)

    candidates = question_candidates(db, _menu(), [_rule()], thresholds={"limit": 4.0})

    assert len(candidates) == 1
    assert candidates[0]["uncovered"] == 2


def test_legacy_say_again_replay_does_not_cover_questions() -> None:
    db = Database(":memory:")
    db.upsert_session(1)
    db.insert_call(
        1,
        {
            "outcome": "fired",
            "call_id": "original",
            "rule_id": "test_rule",
            "t": 1.0,
            "lap": 1,
        },
    )
    db.insert_call(
        1,
        {
            "outcome": "say_again",
            "call_id": "original",
            "rule_id": "test_rule",
            "t": 10.0,
            "lap": 4,
        },
    )
    db.insert_call(
        1,
        {
            "outcome": "fired",
            "call_id": "legacy-replay",
            "rule_id": "test_rule",
            "t": 11.0,
            "lap": 4,
        },
    )
    _ask(db, 1, 5, 12.0, 5.0)
    _ask(db, 1, 6, 13.0, 7.0)

    candidates = question_candidates(db, _menu(), [_rule()], thresholds={"limit": 4.0})

    assert len(candidates) == 1
    assert candidates[0]["uncovered"] == 2


def test_plan_announcement_covers_pit_questions() -> None:
    settings = ConfigStore().current()
    db = Database(":memory:")
    db.upsert_session(1)
    db.insert_call(
        1,
        {
            "outcome": "fired",
            "call_id": "plan-call",
            "rule_id": "plan_announce",
            "t": 9.0,
            "lap": 4,
        },
    )
    _ask(db, 1, 5, 10.0, 5.0, item_id="pit")
    _ask(db, 1, 6, 11.0, 6.0, item_id="pit")

    candidates = question_candidates(
        db,
        settings.menu,
        settings.rules,
        thresholds=settings.thresholds,
    )

    pit = next(item for item in settings.menu.items if item.id == "pit")
    assert {
        "plan_announce",
        "plan_announce_no_stop",
        "plan_status",
        "plan_switch",
        "plan_switch_no_stop",
    } <= set(pit.related_rules)
    assert candidates == []


def test_proposals_keep_question_candidates_review_only() -> None:
    db = Database(":memory:")

    result = propose_thresholds(db, ConfigStore().current())

    assert result["review_required"] is True
    assert result["applied"] is False
    assert result["question_candidates"] == []
