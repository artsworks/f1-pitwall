from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import PolicySettings, RuleDefModel
from pitwall.engine import build_engine, run_replay
from pitwall.learning.bench import evaluate_checks, parse_scenario
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import Snapshot

from .factories import make_snapshot
from .synth import track_limits_scenario, write_packet_stream

CHAIN = ("track_limit_warning", "track_limit_second_warning", "track_limit_last_warning")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("events", "lap_counts", "infringements"),
    [
        (True, True, (27, 28, 29)),
        (False, True, ()),
        (True, False, (27, 28, 29)),
        (True, True, (7, 27, 9)),
    ],
)
def test_warning_chain_replays_once_and_deterministically(
    tmp_path: Path, events: bool, lap_counts: bool, infringements: tuple[int, ...]
) -> None:
    stream = track_limits_scenario(
        events=events, lap_counts=lap_counts, infringements=infringements
    )
    path = write_packet_stream(tmp_path / "track-limits.f1bin", stream, wall_clock_start_us=0)
    runs = []
    for _ in range(2):
        log = io.StringIO()
        engine = build_engine(clock=VirtualClock(), isolated=True, sinks=[], decision_log_fp=log)
        _, calls = asyncio.run(run_replay(path, engine))
        warnings = [call for call in calls if call.rule_id in CHAIN]
        assert [(call.rule_id, call.lap) for call in warnings] == list(
            zip(CHAIN, (2, 3, 4), strict=True)
        )
        for count, call in enumerate(warnings, 1):
            assert f"{count:.1f}" in call.text
            assert call.priority == 3
            assert call.urgency == "info"
            assert "{" not in call.text
        assert "black-and-white" in warnings[1].text
        assert "penalty" in warnings[2].text or "costs time" in warnings[2].text
        assert any(call.rule_id == "penalty_track_limits" and call.lap == 5 for call in calls)
        assert not any(call.rule_id.startswith("corner_cut_") for call in calls)
        rows = [json.loads(line) for line in log.getvalue().splitlines()]
        assert not any(row.get("reason") in ("expr_error", "format_error") for row in rows)
        runs.append([(call.rule_id, call.text, call.t) for call in warnings])
    assert runs[0] == runs[1]


@pytest.mark.parametrize("vehicle_idx,infringements", [(1, (27, 28, 29)), (0, (4, 4, 4))])
def test_rival_and_collision_warnings_do_not_start_chain(
    tmp_path: Path, vehicle_idx: int, infringements: tuple[int, ...]
) -> None:
    path = write_packet_stream(
        tmp_path / "other-warnings.f1bin",
        track_limits_scenario(
            lap_counts=False, vehicle_idx=vehicle_idx, infringements=infringements, penalty=False
        ),
    )
    engine = build_engine(
        clock=VirtualClock(), isolated=True, sinks=[], decision_log_fp=io.StringIO()
    )
    _, calls = asyncio.run(run_replay(path, engine))
    assert not any(call.rule_id in CHAIN for call in calls)


def _rules() -> RuleEngine:
    settings = ConfigStore(isolated=True).current()
    return RuleEngine(
        [rule for rule in settings.rules if rule.id in CHAIN],
        thresholds=settings.thresholds,
        mode={},
        staleness_s={"lap_data": 1.0},
    )


def _snapshot(count: int, *, now: float = 30.0, **kwargs: object) -> Snapshot:
    return make_snapshot(
        session_kind="race",
        session_type=15,
        lap_num=5,
        phase="racing",
        now=now,
        track_warning_count=count,
        _ages={"lap_data": 0.0},
        **kwargs,
    )


def test_joining_at_three_does_not_replay_lower_counts_or_repeat_after_pit() -> None:
    rules = _rules()
    snap = _snapshot(3)
    fired = rules.evaluate(snap).candidates
    assert [candidate.rule.id for candidate in fired] == [CHAIN[2]]
    for now, phase in ((45.0, "racing"), (60.0, "pit_lane"), (75.0, "out_lap"), (90.0, "racing")):
        assert not rules.evaluate(replace(snap, now=now, phase=phase)).candidates


def test_lower_count_calls_expire_and_zero_four_or_more_are_silent() -> None:
    rules = _rules()
    first = rules.evaluate(_snapshot(1)).candidates[0]
    second = rules.evaluate(_snapshot(2, now=45.0)).candidates[0]
    assert first.still_true is not None and not first.still_true(_snapshot(2))
    assert second.still_true is not None and not second.still_true(_snapshot(3))
    for count in (0, 4, 5, 6, 7):
        assert not rules.evaluate(_snapshot(count, now=60.0 + count)).candidates


@pytest.mark.parametrize("phase", ["racing", "sc", "vsc"])
def test_warning_chain_requires_fresh_lap_data_in_race_phases(phase: str) -> None:
    rules = _rules()
    snap = replace(_snapshot(3), phase=phase, _ages={"lap_data": 2.0})
    assert not rules.evaluate(snap).candidates
    assert [
        candidate.rule.id
        for candidate in rules.evaluate(replace(snap, _ages={"lap_data": 0.0})).candidates
    ] == [CHAIN[2]]


@pytest.mark.parametrize("session_kind", ["practice", "qualifying"])
def test_non_race_sessions_do_not_start_chain(session_kind: str) -> None:
    assert not _rules().evaluate(replace(_snapshot(3), session_kind=session_kind)).candidates


def test_count_rollback_rearms_without_inventing_penalty_cycles() -> None:
    rules = _rules()
    for count, now in ((1, 30.0), (2, 45.0), (3, 60.0), (1, 75.0), (2, 90.0)):
        assert [
            candidate.rule.id for candidate in rules.evaluate(_snapshot(count, now=now)).candidates
        ] == [CHAIN[count - 1]]


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("reason", ["budget", "focus"])
@pytest.mark.parametrize("queued", [False, True])
def test_busy_radio_retries_warning_at_same_count_once(
    count: int, reason: str, queued: bool
) -> None:
    rules = _rules()
    log = io.StringIO()
    dispatcher = Dispatcher(
        PolicySettings(calls_per_lap=1, min_gap_s=0, p3_straight_only=False, focus_window_s=5),
        VirtualClock(),
        decision_log=DecisionLog(fp=log),
        sinks=[],
    )
    blocker = RuleEngine(
        [
            RuleDefModel(
                id="blocker",
                priority=1 if reason == "focus" else 2,
                urgency="safety" if reason == "focus" else "tactical",
                when="True",
                say="Yellow flag",
            )
        ],
        thresholds={},
        mode={},
    )
    snap = _snapshot(count)
    busy = replace(snap, now=31.0)
    if queued:
        dispatcher.submit(rules.evaluate(snap).candidates, snap)
        if reason == "budget":
            dispatcher.drop_budgeted()
        else:
            dispatcher.submit(blocker.evaluate(busy).candidates, busy)
            assert [call.rule_id for call in dispatcher.drain(busy.now)] == ["blocker"]
    else:
        dispatcher.submit(blocker.evaluate(snap).candidates, snap)
        assert [call.rule_id for call in dispatcher.drain(snap.now)] == ["blocker"]
        dispatcher.submit(rules.evaluate(busy).candidates, busy)
    assert dispatcher.drain(busy.now) == []
    assert any(
        row["rule_id"] == CHAIN[count - 1] and row.get("suppressed_by") == reason
        for row in map(json.loads, log.getvalue().splitlines())
    )

    available = replace(snap, now=40.0, lap_num=6 if reason == "budget" else 5)
    dispatcher.submit(rules.evaluate(available).candidates, available)
    assert [call.rule_id for call in dispatcher.drain(available.now)] == [CHAIN[count - 1]]
    assert not rules.evaluate(replace(available, now=70.0)).candidates


def test_all_phrase_variants_include_count_and_next_consequence() -> None:
    settings = ConfigStore(isolated=True).current()
    for count, rule_id in enumerate(CHAIN, 1):
        rule = next(rule for rule in settings.rules if rule.id == rule_id)
        assert len(rule.say_pool()) >= 3
        for phrase in rule.say_pool():
            assert "{track_warning_count:.1f}" in phrase
            if count < 3:
                assert "black-and-white" in phrase.lower()
            else:
                assert "penalty" in phrase or "costs time" in phrase


def test_scenario_matches_reproducible_stream(tmp_path: Path) -> None:
    path = write_packet_stream(
        tmp_path / "austria-track-limits.f1bin", track_limits_scenario(), wall_clock_start_us=0
    )
    scenario = parse_scenario(ROOT / "scenarios/austria-track-limits.yaml")
    assert scenario.source_uid == 0xDEADBEEF
    assert scenario.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    engine = build_engine(
        clock=VirtualClock(), isolated=True, sinks=[], decision_log_fp=io.StringIO()
    )
    _, calls = asyncio.run(run_replay(path, engine))
    checks = evaluate_checks(scenario.checks, [(call.rule_id, call.lap) for call in calls])
    assert all(check["ok"] for check in checks), checks
