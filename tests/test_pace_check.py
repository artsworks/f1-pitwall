from dataclasses import replace

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.rules.engine import RuleEngine
from pitwall.state.lap import LapSummary
from pitwall.state.session import SessionState, Snapshot
from tests.race_synth import RaceSpec, replay_race


@pytest.fixture(scope="module")
def pace_runs(tmp_path_factory):
    specs = {
        "catching": RaceSpec(
            laps=10,
            base_ms=95_000,
            gap_ahead_s=3.0,
            gap_behind_s=12.0,
            player_lap_offset_ms=((1, -350),),
        ),
        "fast": RaceSpec(
            laps=10,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
            player_lap_offset_ms=((5, -500), (6, 500), (8, -500), (9, 500)),
        ),
        "slow": RaceSpec(
            laps=10,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
            player_lap_offset_ms=((5, 500), (6, -500), (8, 500), (9, -500)),
        ),
        "dirty": RaceSpec(
            laps=12,
            base_ms=95_000,
            gap_ahead_s=1.2,
            gap_behind_s=12.0,
            player_lap_offset_ms=((5, 500),),
        ),
        "noise": RaceSpec(
            laps=10,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
            player_lap_offset_ms=((5, -50), (6, 50), (8, -50), (9, 50)),
        ),
    }
    return {
        name: replay_race(tmp_path_factory.mktemp(f"pace-{name}"), spec)
        for name, spec in specs.items()
    }


def _snapshot(**changes) -> Snapshot:
    snapshot = Snapshot(
        now=0.0,
        session_kind="race",
        session_type=15,
        phase="racing",
        lap_num=6,
        laps_remaining=12,
        sector=1,
        battle_mode="free_air",
        gap_ahead_s=12.0,
        gap_behind_s=12.0,
        pace_delta_vs_expected_s=-0.3,
        pace_slow_laps=0,
        pace_delta="three tenths",
        position=3,
        rival_behind_idx=2,
        rival_behind_name="LECLERC",
        _ages={"lap_data": 0.1},
    )
    return replace(snapshot, **changes)


def _rule_engine() -> RuleEngine:
    settings = ConfigStore().current()
    return RuleEngine(list(settings.rules), thresholds=settings.thresholds, mode={})


@pytest.mark.parametrize(("valid", "sc_status"), [(False, 0), (True, 1)])
def test_ineligible_lap_resets_pace_streak(valid: bool, sc_status: int) -> None:
    state = SessionState(thresholds={"pace_check_min_delta_s": 0.15})
    state.session_type = 15
    state._pace_prediction_latch = (4, 90_000)
    state.pace_delta_vs_expected_s = 0.5
    state.pace_slow_laps = 2

    state._record_pace_delta(
        LapSummary(
            lap_num=4,
            lap_time_ms=91_000,
            sector1_ms=30_000,
            sector2_ms=30_000,
            compound=17,
            tyre_age_laps=2,
            fuel_remaining_laps_at_end=5.0,
            valid=valid,
            sc_status=sc_status,
        )
    )

    assert state.pace_delta_vs_expected_s == 0.0
    assert state.pace_slow_laps == 0


def test_pace_check_rule_and_templates_render(caplog: pytest.LogCaptureFixture) -> None:
    settings = ConfigStore().current()
    engine = _rule_engine()
    snapshot = _snapshot(position=2, rival_behind_idx=-1)
    result = engine.evaluate(snapshot)
    assert any(candidate.rule.defn.id == "pace_check" for candidate in result.candidates)

    rule = next(rule for rule in settings.rules if rule.id == "pace_check")
    lines = [*rule.say, *(line for tier in rule.severity for line in tier.say)]
    snapshots = [
        _snapshot(position=1, rival_behind_idx=2, pace_delta_vs_expected_s=-0.3),
        _snapshot(position=1, rival_behind_idx=2, pace_delta_vs_expected_s=0.3),
        _snapshot(position=2, rival_behind_idx=-1, pace_delta_vs_expected_s=0.3),
        _snapshot(position=2, rival_behind_idx=-1, pace_delta_vs_expected_s=-0.3),
    ]
    for snapshot in snapshots:
        for line in lines:
            candidate = engine.candidate_for("pace_check", snapshot, text=line)
            assert "{" not in candidate.text

    assert not any(record.getMessage().startswith("rule pace_check") for record in caplog.records)


def test_pace_check_waits_for_dirty_air_slow_laps() -> None:
    dirty_air = _snapshot(
        lap_num=6,
        battle_mode="managing",
        gap_ahead_s=1.2,
        pace_delta_vs_expected_s=0.3,
        pace_slow_laps=1,
    )
    early = _rule_engine().evaluate(dirty_air)
    assert not any(candidate.rule.defn.id == "pace_check" for candidate in early.candidates)

    after_streak = replace(dirty_air, now=1.0, pace_slow_laps=3)
    later = _rule_engine().evaluate(after_streak)
    assert any(candidate.rule.defn.id == "pace_check" for candidate in later.candidates)


@pytest.mark.replay
def test_battle_catching_call_includes_catch_laps(pace_runs) -> None:
    calls, rows = pace_runs["catching"]
    call = next(call for call in calls if call.rule_id == "battle_catching")
    assert call.inputs["battle_catch_laps"] > 2
    assert "slower" in call.text.lower()
    assert "on him in" in call.text.lower()
    row = next(row for row in rows if row.get("rule_id") == "battle_catching")
    assert row["inputs"]["battle_mode"] == "catching"


@pytest.mark.replay
def test_free_air_pace_check_reports_fast_and_slow_pace(pace_runs) -> None:
    fast_calls, _ = pace_runs["fast"]
    fast = [call for call in fast_calls if call.rule_id == "pace_check"]
    assert len(fast) >= 2
    assert all(call.inputs["lap_num"] % 3 == 0 for call in fast)
    assert all(call.inputs["pace_delta_vs_expected_s"] <= -0.3 for call in fast)
    assert all(
        "under expected" in call.text.lower() or "quicker than expected" in call.text.lower()
        for call in fast
    )

    slow_calls, _ = pace_runs["slow"]
    slow = [call for call in slow_calls if call.rule_id == "pace_check"]
    assert slow
    assert all(call.inputs["lap_num"] % 3 == 0 for call in slow)
    assert all(call.inputs["pace_delta_vs_expected_s"] >= 0.15 for call in slow)
    assert all(
        "off expected" in call.text.lower() or "slower than expected" in call.text.lower()
        for call in slow
    )


@pytest.mark.replay
def test_dirty_air_and_noise_suppress_pace_check(pace_runs) -> None:
    dirty_calls, dirty_rows = pace_runs["dirty"]
    slow_calls = [call for call in dirty_calls if call.rule_id == "pace_check"]
    assert not any(call.inputs["lap_num"] == 6 for call in slow_calls)
    assert all(call.inputs["pace_slow_laps"] >= 3 for call in slow_calls)
    assert any(
        row.get("rule_id") == "battle_manage"
        and row.get("inputs", {}).get("battle_mode") == "managing"
        for row in dirty_rows
    )

    noise_calls, _ = pace_runs["noise"]
    assert not any(call.rule_id == "pace_check" for call in noise_calls)
