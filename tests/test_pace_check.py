from dataclasses import replace

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.rules.engine import RuleEngine
from pitwall.state.lap import LapSummary
from pitwall.state.session import SessionState, Snapshot
from tests.race_synth import RaceSpec, replay_race


def _replay_with_lap_states(directory, spec):
    lap_states = []
    original = SessionState._record_pace_delta

    def record_pace_delta(state: SessionState, summary: LapSummary) -> None:
        original(state, summary)
        lap_states.append(
            (
                summary.lap_num,
                summary.valid,
                summary.sc_status,
                bool(summary.traffic),
                state.pace_delta_vs_expected_s,
                state._model.deg_fit_source,
            )
        )

    SessionState._record_pace_delta = record_pace_delta
    try:
        calls, rows = replay_race(directory, spec)
    finally:
        SessionState._record_pace_delta = original
    return calls, rows, lap_states


def _assert_pace_calls_after_six_usable_laps(calls, lap_states) -> None:
    pace_calls = [call for call in calls if call.rule_id == "pace_check"]
    assert pace_calls
    for call in pace_calls:
        assert call.inputs["deg_fit_source"] == "fit"
        usable_laps = sum(
            valid and sc_status == 0 and not traffic and completed_lap < call.inputs["lap_num"]
            for completed_lap, valid, sc_status, traffic, _, _ in lap_states
        )
        assert usable_laps >= 6


def _completed_lap_state(lap_states, lap_num):
    return next(state for state in lap_states if state[0] == lap_num)


@pytest.fixture(scope="module")
def pace_runs(tmp_path_factory):
    specs = {
        "catching": RaceSpec(
            laps=10,
            base_ms=95_000,
            gap_ahead_s=2.5,
            gap_behind_s=12.0,
            player_lap_offset_ms=((1, -350),),
        ),
        "fast": RaceSpec(
            laps=13,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
            player_lap_offset_ms=((5, -500), (6, 500), (8, -500), (9, 500), (11, -500), (12, 500)),
        ),
        "slow": RaceSpec(
            laps=13,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
            player_lap_offset_ms=((5, 500), (6, -500), (8, 500), (9, -500), (11, 500), (12, -500)),
        ),
        "dirty": RaceSpec(
            laps=15,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_ahead_changes=((8, 1.2),),
            gap_behind_s=12.0,
            player_lap_offset_ms=((8, 500),),
        ),
        "traffic_cleared": RaceSpec(
            laps=15,
            base_ms=95_000,
            gap_ahead_s=12.0,
            gap_ahead_changes=((8, 1.0), (9, 8.0)),
            gap_behind_s=12.0,
            player_lap_offset_ms=((8, 500), (9, -500), (11, 500), (12, -500)),
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
        name: _replay_with_lap_states(tmp_path_factory.mktemp(f"pace-{name}"), spec)
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
        deg_fit_source="fit",
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


def test_record_pace_delta_copies_lap_traffic() -> None:
    state = SessionState(thresholds={"pace_check_min_delta_s": 0.15})
    state.session_type = 15
    for traffic in (1, 0):
        state._pace_prediction_latch = (4, 90_000)
        state._record_pace_delta(
            LapSummary(
                lap_num=4,
                lap_time_ms=90_500,
                sector1_ms=30_000,
                sector2_ms=30_000,
                compound=17,
                tyre_age_laps=2,
                fuel_remaining_laps_at_end=5.0,
                valid=True,
                traffic=traffic,
            )
        )
        assert state.pace_lap_traffic is bool(traffic)


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


def test_pace_check_waits_for_settled_degradation_fit() -> None:
    engine = _rule_engine()
    blend = _snapshot(deg_fit_source="blend")
    result = engine.evaluate(blend)
    assert not any(candidate.rule.defn.id == "pace_check" for candidate in result.candidates)

    fitted = replace(blend, now=1.0, deg_fit_source="fit")
    result = engine.evaluate(fitted)
    assert any(candidate.rule.defn.id == "pace_check" for candidate in result.candidates)


def test_pace_check_waits_for_dirty_air_slow_laps() -> None:
    dirty_air = _snapshot(
        lap_num=6,
        battle_mode="managing",
        gap_ahead_s=1.2,
        pace_lap_traffic=True,
        pace_delta_vs_expected_s=0.3,
        pace_slow_laps=1,
    )
    early = _rule_engine().evaluate(dirty_air)
    assert not any(candidate.rule.defn.id == "pace_check" for candidate in early.candidates)

    after_streak = replace(dirty_air, now=1.0, pace_slow_laps=3)
    later = _rule_engine().evaluate(after_streak)
    assert any(candidate.rule.defn.id == "pace_check" for candidate in later.candidates)


def test_pace_check_waits_when_traffic_clears_after_slow_lap() -> None:
    cleared = _snapshot(
        gap_ahead_s=8.0,
        pace_lap_traffic=True,
        pace_delta_vs_expected_s=0.4,
        pace_slow_laps=1,
    )
    early = _rule_engine().evaluate(cleared)
    assert not any(candidate.rule.defn.id == "pace_check" for candidate in early.candidates)

    after_streak = replace(cleared, now=1.0, pace_slow_laps=3)
    later = _rule_engine().evaluate(after_streak)
    assert any(candidate.rule.defn.id == "pace_check" for candidate in later.candidates)


@pytest.mark.replay
def test_battle_catching_call_includes_catch_laps(pace_runs) -> None:
    calls, rows, _ = pace_runs["catching"]
    call = next(call for call in calls if call.rule_id == "battle_catching")
    assert call.inputs["battle_catch_laps"] > 2
    assert "slower" in call.text.lower()
    assert "on him in" in call.text.lower()
    row = next(row for row in rows if row.get("rule_id") == "battle_catching")
    assert row["inputs"]["battle_mode"] == "catching"


@pytest.mark.replay
def test_free_air_pace_check_reports_fast_and_slow_pace(pace_runs) -> None:
    fast_calls, _, fast_lap_states = pace_runs["fast"]
    fast = [call for call in fast_calls if call.rule_id == "pace_check"]
    assert len(fast) >= 2
    _assert_pace_calls_after_six_usable_laps(fast_calls, fast_lap_states)
    assert all(call.inputs["lap_num"] % 3 == 0 for call in fast)
    assert all(call.inputs["lap_num"] >= 9 for call in fast)
    assert all(call.inputs["pace_delta_vs_expected_s"] <= -0.3 for call in fast)
    assert all(
        "under expected" in call.text.lower() or "quicker than expected" in call.text.lower()
        for call in fast
    )

    slow_calls, _, slow_lap_states = pace_runs["slow"]
    slow = [call for call in slow_calls if call.rule_id == "pace_check"]
    assert slow
    _assert_pace_calls_after_six_usable_laps(slow_calls, slow_lap_states)
    assert all(call.inputs["lap_num"] % 3 == 0 for call in slow)
    assert all(call.inputs["lap_num"] >= 9 for call in slow)
    assert all(call.inputs["pace_delta_vs_expected_s"] >= 0.15 for call in slow)
    assert all(
        "off expected" in call.text.lower() or "slower than expected" in call.text.lower()
        for call in slow
    )


@pytest.mark.replay
def test_dirty_air_and_noise_suppress_pace_check(pace_runs) -> None:
    dirty_calls, dirty_rows, dirty_lap_states = pace_runs["dirty"]
    slow_calls = [call for call in dirty_calls if call.rule_id == "pace_check"]
    lap_eight = _completed_lap_state(dirty_lap_states, 8)
    assert lap_eight[3] is True
    assert lap_eight[4] >= 0.15
    assert lap_eight[5] == "fit"
    assert not any(call.inputs["lap_num"] == 9 for call in slow_calls)
    assert slow_calls
    assert all(call.inputs["pace_slow_laps"] >= 3 for call in slow_calls)
    assert all(call.inputs["deg_fit_source"] == "fit" for call in slow_calls)
    assert any(call.inputs["lap_num"] >= 12 for call in slow_calls)
    _assert_pace_calls_after_six_usable_laps(dirty_calls, dirty_lap_states)
    assert any(
        row.get("rule_id") == "battle_manage"
        and row.get("inputs", {}).get("battle_mode") == "managing"
        for row in dirty_rows
    )

    noise_calls, _, noise_lap_states = pace_runs["noise"]
    assert any(state[5] == "fit" for state in noise_lap_states)
    assert not any(call.rule_id == "pace_check" for call in noise_calls)


@pytest.mark.replay
def test_on_model_degradation_reads_on_expected_pace(tmp_path, monkeypatch) -> None:
    observed = []
    original = SessionState._record_pace_delta

    def record_pace_delta(self: SessionState, summary: LapSummary) -> None:
        original(self, summary)
        observed.append(
            (summary.lap_num, self.pace_delta_vs_expected_s, self._model.deg_fit_source)
        )

    monkeypatch.setattr(SessionState, "_record_pace_delta", record_pace_delta)
    calls, _ = replay_race(
        tmp_path,
        RaceSpec(
            laps=12,
            base_ms=95_000,
            deg_ms=300,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
        ),
    )

    fitted = [row for row in observed if row[2] == "fit"]
    assert len(fitted) >= 3
    assert all(abs(delta) < 0.05 for _, delta, _ in fitted)
    pace_calls = [call for call in calls if call.rule_id == "pace_check"]
    assert all(call.inputs["deg_fit_source"] == "fit" for call in pace_calls)
    unsettled_slow = [
        row for row in observed if row[2] != "fit" and abs(row[1]) >= 0.15 and (row[0] + 1) % 3 == 0
    ]
    assert unsettled_slow
    assert not any(
        call.inputs["lap_num"] == lap_num + 1
        for call in pace_calls
        for lap_num, _, _ in unsettled_slow
    )
    assert not any(call.inputs["lap_num"] >= 9 for call in pace_calls)


@pytest.mark.replay
def test_fuel_forecast_uses_only_fit_usable_laps(tmp_path, monkeypatch) -> None:
    observed = []
    original = SessionState._record_pace_delta

    def record_pace_delta(self: SessionState, summary: LapSummary) -> None:
        original(self, summary)
        observed.append(
            (summary.lap_num, self.pace_delta_vs_expected_s, self._model.deg_fit_source)
        )

    monkeypatch.setattr(SessionState, "_record_pace_delta", record_pace_delta)
    calls, _ = replay_race(
        tmp_path,
        RaceSpec(
            laps=15,
            base_ms=95_000,
            deg_ms=300,
            gap_ahead_s=12.0,
            gap_behind_s=12.0,
            sc_laps=(2, 4),
            fuel_ms_per_kg=30.0 / 1.7,
        ),
    )

    fitted = [row for row in observed if row[2] == "fit"]
    assert len(fitted) >= 3
    assert max(abs(delta) for _, delta, _ in fitted) < 0.05
    assert not any(
        call.rule_id == "pace_check" and call.inputs["pace_delta_vs_expected_s"] >= 0.15
        for call in calls
    )


@pytest.mark.replay
def test_pace_fit_works_without_persistence(tmp_path, monkeypatch) -> None:
    original = SessionState._record_pace_delta
    spec = RaceSpec(
        laps=15,
        base_ms=95_000,
        gap_ahead_s=12.0,
        gap_behind_s=12.0,
        player_lap_offset_ms=((11, 500), (12, -500)),
    )

    def run(name: str, persist: bool):
        observed = []

        def record_pace_delta(state: SessionState, summary: LapSummary) -> None:
            original(state, summary)
            observed.append(
                (summary.lap_num, state.pace_delta_vs_expected_s, state._model.deg_fit_source)
            )

        monkeypatch.setattr(SessionState, "_record_pace_delta", record_pace_delta)
        path = tmp_path / name
        path.mkdir()
        calls, _ = replay_race(path, spec, persist=persist)
        return calls, observed

    calls_no_persistence, no_persistence_states = run("no-persistence", False)
    assert any(source == "fit" for _, _, source in no_persistence_states)
    slow_call = next(
        call
        for call in calls_no_persistence
        if call.rule_id == "pace_check" and call.inputs["lap_num"] == 12
    )
    assert slow_call.inputs["pace_delta_vs_expected_s"] >= 0.15
    assert slow_call.inputs["deg_fit_source"] == "fit"

    calls_persisted, persisted_states = run("persisted", True)
    no_persistence_lap11 = next(row for row in no_persistence_states if row[0] == 11)
    persisted_lap11 = next(row for row in persisted_states if row[0] == 11)
    assert abs(no_persistence_lap11[1] - persisted_lap11[1]) < 0.01
    assert no_persistence_lap11[2] == persisted_lap11[2] == "fit"
    assert any(call.rule_id == "pace_check" for call in calls_persisted)


@pytest.mark.replay
def test_pace_check_uses_traffic_of_completed_lap(pace_runs) -> None:
    calls, _, lap_states = pace_runs["traffic_cleared"]
    slow_calls = [
        call
        for call in calls
        if call.rule_id == "pace_check" and call.inputs["pace_delta_vs_expected_s"] > 0
    ]
    lap_eight = _completed_lap_state(lap_states, 8)
    assert lap_eight[3] is True
    assert lap_eight[4] >= 0.15
    assert lap_eight[5] == "fit"
    assert not any(call.inputs["lap_num"] == 9 for call in slow_calls)
    assert any(
        call.inputs["lap_num"] >= 12
        and not call.inputs.get("pace_lap_traffic", False)
        and call.inputs["deg_fit_source"] == "fit"
        for call in slow_calls
    )
    assert all(
        not call.inputs.get("pace_lap_traffic", False) or call.inputs["pace_slow_laps"] >= 3
        for call in slow_calls
    )
    _assert_pace_calls_after_six_usable_laps(calls, lap_states)
