from __future__ import annotations

import pytest

from .race_synth import RaceSpec, replay_race

pytestmark = [pytest.mark.slow, pytest.mark.replay]

NOT_REPLAYABLE: dict[str, str] = {}

_BASE_POSITIONS = ((3, 2, 4, 1),) * 8
_PASS_POSITIONS = (
    (3, 2, 4, 1),
    (3, 2, 4, 1),
    (2, 3, 4, 1),
    (2, 3, 4, 1),
)
_LOST_POSITIONS = (
    (3, 2, 4, 1),
    (3, 2, 4, 1),
    (4, 2, 3, 1),
    (4, 2, 3, 1),
)
_SWAP_AHEAD_POSITIONS = (
    (3, 2, 4, 1),
    (3, 2, 4, 1),
    (2, 3, 4, 1),
    (3, 2, 4, 1),
    (2, 3, 4, 1),
    (3, 2, 4, 1),
    (2, 3, 4, 1),
    (2, 3, 4, 1),
)
_SWAP_BEHIND_POSITIONS = (
    (3, 2, 4, 1),
    (3, 2, 4, 1),
    (4, 2, 3, 1),
    (3, 2, 4, 1),
    (4, 2, 3, 1),
    (3, 2, 4, 1),
    (4, 2, 3, 1),
    (4, 2, 3, 1),
)
_SCRAP_POSITIONS = (
    (3, 2, 4, 1),
    (2, 3, 4, 1),
    (3, 2, 4, 1),
    (4, 2, 3, 1),
    (3, 2, 4, 1),
    (3, 2, 4, 1),
)

_CASES = [
    (
        "release_too_late",
        RaceSpec(laps=2, session_type=5, quali_garage=True, session_time_left_s=10),
    ),
    (
        "release_window_closing",
        RaceSpec(laps=2, session_type=5, quali_garage=True, session_time_left_s=170),
    ),
    (
        "tyre_overheat_wet",
        RaceSpec(laps=8, base_ms=12_000, compound=7, hot_tyres_lap=2),
    ),
    (
        "tyre_switch",
        RaceSpec(
            laps=14,
            base_ms=90_000,
            compound=7,
            extra={
                "car_status": {
                    "cars": {
                        1: {
                            "actual_tyre_compound": 16,
                            "visual_tyre_compound": 16,
                            "tyres_age_laps": 2,
                        },
                        2: {
                            "actual_tyre_compound": 7,
                            "visual_tyre_compound": 7,
                            "tyres_age_laps": 2,
                        },
                        3: {
                            "actual_tyre_compound": 16,
                            "visual_tyre_compound": 16,
                            "tyres_age_laps": 2,
                        },
                    }
                },
                "lap_data": {
                    "cars": {
                        1: {"last_lap_time_ms": 85_000},
                        2: {"last_lap_time_ms": 95_000},
                        3: {"last_lap_time_ms": 85_000},
                    }
                },
            },
        ),
    ),
    (
        "tyre_switch_slicks_traffic",
        RaceSpec(
            laps=14,
            base_ms=90_000,
            compound=7,
            gap_ahead_s=0.8,
            extra={
                "car_status": {
                    "cars": {
                        1: {
                            "actual_tyre_compound": 16,
                            "visual_tyre_compound": 16,
                            "tyres_age_laps": 2,
                        },
                        2: {
                            "actual_tyre_compound": 7,
                            "visual_tyre_compound": 7,
                            "tyres_age_laps": 2,
                        },
                        3: {
                            "actual_tyre_compound": 16,
                            "visual_tyre_compound": 16,
                            "tyres_age_laps": 2,
                        },
                    }
                },
                "lap_data": {
                    "cars": {
                        1: {"last_lap_time_ms": 85_000},
                        # Parked just short of the projected rejoin so the pit
                        # exit is never clean (driver_status 1 = flying lap so
                        # release_window counts it).
                        2: {
                            "last_lap_time_ms": 95_000,
                            "lap_distance": 3_800,
                            "driver_status": 1,
                        },
                        3: {"last_lap_time_ms": 85_000},
                    }
                },
            },
        ),
    ),
    (
        "weather_box_now",
        RaceSpec(
            laps=15,
            base_ms=90_000,
            compound=17,
            rain_lap=2,
            rain_percentages=(100, 100),
            traction_laps=(3,),
            traction_bursts=3,
            extra={
                "motion_ex": {},
                "car_status": {
                    "cars": {
                        1: {
                            "actual_tyre_compound": 7,
                            "visual_tyre_compound": 7,
                            "tyres_age_laps": 2,
                        },
                        2: {
                            "actual_tyre_compound": 16,
                            "visual_tyre_compound": 16,
                            "tyres_age_laps": 2,
                        },
                        3: {
                            "actual_tyre_compound": 7,
                            "visual_tyre_compound": 7,
                            "tyres_age_laps": 2,
                        },
                    }
                },
                "lap_data": {
                    "cars": {
                        1: {"last_lap_time_ms": 86_000},
                        2: {"last_lap_time_ms": 90_000},
                        3: {"last_lap_time_ms": 86_000},
                    }
                },
            },
        ),
    ),
    (
        "tyre_graining",
        RaceSpec(laps=8, base_ms=12_000, tyre_inner_profile=(70, 70, 70, 70)),
    ),
    (
        "tyre_blister",
        RaceSpec(
            laps=8,
            base_ms=90_000,
            extra={"car_damage": {"cars": {0: {"tyre_blisters": (100, 0, 0, 0)}}}},
        ),
    ),
    (
        "energy_over",
        RaceSpec(laps=8, base_ms=12_000, ers_deployed_j_per_lap=10_000_000.0),
    ),
    (
        "battle_manage",
        RaceSpec(
            laps=12,
            base_ms=12_000,
            gap_ahead_s=2.5,
            gap_behind_s=8.0,
        ),
    ),
    (
        "battle_passed",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            positions_by_lap=_PASS_POSITIONS,
            gap_ahead_s=0.5,
        ),
    ),
    (
        "battle_held",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            positions_by_lap=_BASE_POSITIONS,
            gap_behind_s=0.5,
            gap_behind_from_lap=(4, 1.5),
        ),
    ),
    (
        "battle_lost",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            positions_by_lap=_LOST_POSITIONS,
            gap_behind_s=0.5,
        ),
    ),
    (
        "off_track_lost",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            off_track_lap=3,
            off_track_position_changes=((0.35, 4),),
        ),
    ),
    (
        "battle_swap_ahead",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            positions_by_lap=_SWAP_AHEAD_POSITIONS,
            gap_ahead_s=0.5,
        ),
    ),
    (
        "battle_swap_behind",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            positions_by_lap=_SWAP_BEHIND_POSITIONS,
            gap_behind_s=0.5,
        ),
    ),
    (
        "battle_scrap",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            positions_by_lap=_SCRAP_POSITIONS,
            gap_ahead_s=0.5,
            gap_behind_s=0.5,
        ),
    ),
    (
        "penalty_pit",
        RaceSpec(laps=8, base_ms=12_000, penalty_lap=3, penalty_type=0),
    ),
    (
        "penalty_track_limits",
        RaceSpec(laps=8, base_ms=12_000, penalty_lap=3, penalty_type=4, penalty_infringement=21),
    ),
    (
        "track_limit_warning",
        RaceSpec(laps=8, base_ms=12_000, warning_laps=((3, 27),)),
    ),
    (
        "track_limit_last_warning",
        RaceSpec(laps=8, base_ms=12_000, warning_laps=((2, 27), (3, 27), (4, 27))),
    ),
    (
        "corner_cut_warning",
        RaceSpec(laps=8, base_ms=12_000, warning_laps=((3, 7),)),
    ),
    (
        "corner_cut_last_warning",
        RaceSpec(laps=8, base_ms=12_000, warning_laps=((2, 7), (3, 7), (4, 7))),
    ),
    (
        "track_limit_overtake",
        RaceSpec(laps=8, base_ms=12_000, warning_laps=((3, 8),)),
    ),
    (
        "rival_behind_pitted",
        RaceSpec(laps=8, base_ms=12_000, rival_behind_pit_lap=3, gap_behind_s=1.0),
    ),
    (
        "weather_to_wet",
        RaceSpec(laps=8, base_ms=12_000, rain_lap=3, rain_percentages=(100, 100)),
    ),
    (
        "weather_to_dry",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            compound=7,
            rain_lap=2,
            rain_end_lap=5,
            rain_percentages=(100, 100),
        ),
    ),
    (
        "rear_wing_damage",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            extra={"car_damage": {"cars": {0: {"rear_wing_damage": 100}}}},
        ),
    ),
    (
        "engine_damage",
        RaceSpec(
            laps=8,
            base_ms=12_000,
            extra={"car_damage": {"cars": {0: {"engine_damage": 100}}}},
        ),
    ),
]


@pytest.mark.parametrize(
    ("rule_id", "spec"),
    _CASES,
    ids=[rule_id for rule_id, _ in _CASES],
)
def test_rule_fires_in_replay(tmp_path, rule_id: str, spec: RaceSpec) -> None:
    _, rows = replay_race(tmp_path, spec)
    fired_rules = [row.get("rule_id") for row in rows if row.get("outcome") == "fired"]
    assert any(row.get("rule_id") == rule_id and row.get("outcome") == "fired" for row in rows), (
        f"{rule_id}: fired rules were {fired_rules}"
    )
