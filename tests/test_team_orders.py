from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.protocol.enums import DriverStatus
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import CarLap, Participant, SessionState, Snapshot

from .race_synth import RaceSpec, replay_race


def _team_state(
    *,
    player_age: int = 0,
    mate_age: int | None = 0,
    compounds: tuple[int, int] = (17, 17),
    stops: tuple[int, int] = (0, 0),
) -> SessionState:
    state = SessionState(thresholds={"plan_two_compound_rule": 0, "teammate_split_age_laps": 5})
    state._player_idx = 0
    state.session_type = 15
    state.race_phase = "racing"
    state.position = 2
    state.lap_distance = 3000.0
    state.track_length_m = 5000.0
    state.speed_kmh = 200.0
    state.tyre_compound = compounds[0]
    state.tyre_age_laps = player_age
    state.num_pit_stops = stops[0]
    state.cars_status = tuple(
        SimpleNamespace(visual_tyre_compound=compound) for compound in compounds
    )
    state.participants = (
        Participant(name="PLAYER", team_id=4),
        Participant(name="MATE", team_id=4),
    )
    state.cars_lap = (
        CarLap(
            lap_distance=3000.0,
            total_distance=100_000.0,
            car_position=2,
            result_status=2,
            driver_status=DriverStatus.FLYING_LAP,
            num_pit_stops=stops[0],
        ),
        CarLap(
            lap_distance=3026.3,
            total_distance=100_026.3,
            car_position=1,
            result_status=2,
            driver_status=DriverStatus.FLYING_LAP,
            num_pit_stops=stops[1],
        ),
    )
    if mate_age is not None:
        state._histories[1] = SimpleNamespace(
            num_laps=mate_age,
            num_tyre_stints=1,
            tyre_stints=(SimpleNamespace(end_lap=255),),
        )
    return state


def _teammate_snapshot(state: SessionState) -> Snapshot:
    view = state._teammate_view(
        1,
        -1,
        0.6,
        math.inf,
        lambda index: state.participants[index].name,
        state.cars_lap,
        55.0,
    )
    return Snapshot(now=0.0, **view)


def test_teammate_ahead_fields_and_safe_defaults() -> None:
    snapshot = _teammate_snapshot(_team_state())
    no_teammate = Snapshot(now=0.0)

    assert snapshot.teammate_ahead
    assert snapshot.teammate_name == "MATE"
    assert snapshot.teammate_gap_s == 0.6
    assert snapshot.teammate_compound == 17
    assert snapshot.teammate_compound_name == "medium"
    assert snapshot.teammate_tyre_age == 0
    assert snapshot.teammate_stops == 0
    assert not snapshot.teammate_split
    assert not no_teammate.teammate_ahead
    assert no_teammate.teammate_tyre_age == -1
    assert not no_teammate.teammate_split


def test_teammate_split_ignores_young_one_lap_pit_offset() -> None:
    snapshot = _teammate_snapshot(_team_state(player_age=2, mate_age=1, stops=(1, 0)))

    assert not snapshot.teammate_split


def test_teammate_split_detects_different_compound_after_five_laps() -> None:
    snapshot = _teammate_snapshot(_team_state(player_age=5, mate_age=6, compounds=(17, 16)))

    assert snapshot.teammate_split
    assert snapshot.teammate_compound_name == "soft"
    assert snapshot.teammate_tyre_age == 6


def test_teammate_split_stays_false_when_age_is_unknown() -> None:
    snapshot = _teammate_snapshot(_team_state(player_age=6, mate_age=None, compounds=(17, 16)))

    assert snapshot.teammate_tyre_age == -1
    assert not snapshot.teammate_split


def test_traffic_ahead_names_teammate() -> None:
    state = _team_state()

    traffic = state._traffic(90_000)
    snapshot = Snapshot(now=0.0, **traffic)

    assert snapshot.traffic_ahead_name == "MATE"
    assert snapshot.traffic_ahead_teammate


@pytest.mark.replay
def test_teammate_hold_only_fires_in_last_three_laps(tmp_path) -> None:
    _, rows = replay_race(
        tmp_path,
        RaceSpec(laps=10, base_ms=12_000, gap_ahead_s=0.6, team_ids=(4, 4, 2, 3)),
    )
    fired = [
        row
        for row in rows
        if row.get("outcome") == "fired"
        and row.get("rule_id") in {"teammate_hold", "teammate_fight"}
    ]
    hold_rows = [row for row in fired if row.get("rule_id") == "teammate_hold"]
    fight_rows = [row for row in fired if row.get("rule_id") == "teammate_fight"]

    assert hold_rows
    assert all((row.get("inputs") or {}).get("laps_remaining", 99) <= 3 for row in hold_rows)
    assert any(row.get("lap", 99) < 7 for row in fight_rows)
    assert not any(row.get("lap", 0) >= 7 for row in fight_rows)


@pytest.mark.replay
def test_teammate_strategy_split_replays_with_known_age(tmp_path) -> None:
    _, rows = replay_race(
        tmp_path,
        RaceSpec(
            laps=8,
            base_ms=12_000,
            team_ids=(4, 4, 2, 3),
            extra={
                "car_status": {
                    "cars": {
                        1: {
                            "visual_tyre_compound": 16,
                            "fuel_in_tank": 20.0,
                            "ers_store_energy": 1_000_000.0,
                        },
                        2: {"fuel_in_tank": 20.0, "ers_store_energy": 1_000_000.0},
                        3: {"fuel_in_tank": 20.0, "ers_store_energy": 1_000_000.0},
                    }
                },
                "car_damage": {
                    "cars": {
                        1: {"tyres_wear": (1.0, 1.0, 1.0, 1.0)},
                        2: {"tyres_wear": (1.0, 1.0, 1.0, 1.0)},
                        3: {"tyres_wear": (1.0, 1.0, 1.0, 1.0)},
                    }
                },
            },
        ),
    )

    assert any(
        row.get("outcome") == "fired" and row.get("rule_id") == "teammate_strategy_split"
        for row in rows
    )


def test_teammate_tow_rule_uses_snapshot_fields() -> None:
    config = ConfigStore().current()
    rule = next(rule for rule in config.rules if rule.when.endswith("traffic_ahead_teammate"))
    engine = RuleEngine([rule], thresholds=config.thresholds, mode={})
    snapshot = Snapshot(
        now=1.0,
        session_kind="qualifying",
        phase="out_lap",
        dist_to_line_m=500.0,
        traffic_ahead_kind="flying",
        traffic_ahead_s=1.1,
        traffic_ahead_name="MATE",
        traffic_ahead_teammate=True,
        _ages={"lap_data": 0.0, "participants": 0.0},
    )

    result = engine.evaluate(snapshot)

    assert len(result.candidates) == 1
    assert result.candidates[0].rule.id.endswith("_teammate")
    assert result.candidates[0].text.startswith("MATE 1.1 ahead")
