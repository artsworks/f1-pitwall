from __future__ import annotations

from dataclasses import replace

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.protocol.header import PacketId
from pitwall.protocol.packets import parse
from pitwall.rules.engine import RuleEngine
from pitwall.rules.expr import Predicate, make_namespace
from pitwall.state.session import SessionState
from pitwall.strategy.weather import best_rain_stop, dry_to_wet_cost_s, wet_by_lap

from .synth import pack_packet


def test_wet_by_lap_interpolates_forecast_samples_and_holds_last_sample() -> None:
    samples = [(0, 0), (2, 80)]
    assert wet_by_lap(samples, lap_s=60, lap_frac=0, laps=4) == [0.4, 0.8, 0.8, 0.8]


def test_wet_by_lap_is_cumulative_and_handles_no_samples() -> None:
    assert wet_by_lap([(0, 20), (2, 0), (4, 80)], 60, 0, 4) == [0.1, 0.1, 0.4, 0.8]
    assert wet_by_lap([], 80, 0, 3) == [0.0, 0.0, 0.0]


def test_dry_to_wet_expected_costs() -> None:
    assert dry_to_wet_cost_s([0.0, 0.0], None, 22.0, 11.1, 4.4) == 0.0
    assert dry_to_wet_cost_s([0.0, 0.5], None, 22.0, 11.1, 4.4) == pytest.approx(5.55)
    assert dry_to_wet_cost_s([0.0, 0.5], 0, 22.0, 11.1, 4.4) == pytest.approx(24.2)


def test_best_rain_stop_matches_strong_and_light_forecasts() -> None:
    strong = [0.0, *([0.86] * 16)]
    light = [0.0, *([0.62] * 16)]
    stop_lap, gain = best_rain_stop(strong, 22.0, 11.1, 4.4)
    assert stop_lap == 0
    assert gain == pytest.approx(3.4, abs=0.1)
    assert best_rain_stop(light, 22.0, 11.1, 4.4) == (-1, 0.0)


def test_session_snapshot_recomputes_and_resets_rain_stop() -> None:
    session_data = {
        "weather": 0,
        "session_type": 15,
        "total_laps": 17,
        "track_length": 5000,
        "num_weather_forecast_samples": 3,
        "weather_forecast_samples": {
            0: {"session_type": 15, "time_offset": 0, "weather": 0, "rain_percentage": 0},
            1: {"session_type": 15, "time_offset": 1, "weather": 0, "rain_percentage": 0},
            2: {"session_type": 15, "time_offset": 2, "weather": 3, "rain_percentage": 86},
        },
    }
    state = SessionState(
        thresholds={
            "pit_loss_default_s": 22.0,
            "slick_wet_loss_pct": 15,
            "inter_dry_loss_pct": 6,
            "rain_preempt_margin_s": 2.0,
            "rain_wet_pct": 85,
        }
    )
    state._best_laps[0] = 74_000
    state.lap_num = 1
    state.lap_distance = 1000
    state.tyre_visual = 16
    state._on_session(parse(PacketId.SESSION, pack_packet(PacketId.SESSION, session_data)))

    snapshot = state.snapshot(0)
    assert snapshot.forecast_accuracy == 0
    assert snapshot.rain_onset_min == 2
    assert snapshot.rain_box_lap == 0
    assert snapshot.rain_box_tyre == "wets"
    assert snapshot.rain_box_gain_s == 3.4

    state._on_session(
        parse(
            PacketId.SESSION,
            pack_packet(PacketId.SESSION, {**session_data, "weather": 3}),
        )
    )
    assert state.rain_onset_min == 0
    assert state.rain_box_lap == -1
    assert state.rain_box_tyre == ""
    assert state.rain_box_gain_s == 0.0

    state.tyre_visual = 7
    state._on_session(parse(PacketId.SESSION, pack_packet(PacketId.SESSION, session_data)))
    assert state.rain_onset_min == 2
    assert state.rain_box_lap == -1
    assert state.rain_box_tyre == ""
    assert state.rain_box_gain_s == 0.0

    inters_state = SessionState(
        thresholds={
            "pit_loss_default_s": 22.0,
            "slick_wet_loss_pct": 40,
            "inter_dry_loss_pct": 6,
            "rain_preempt_margin_s": 2.0,
            "rain_wet_pct": 85,
        }
    )
    inters_state._best_laps[0] = 74_000
    inters_state.lap_num = 1
    inters_state.lap_distance = 1000
    inters_state.tyre_visual = 16
    inters_data = {
        **session_data,
        "total_laps": 4,
        "weather_forecast_samples": {
            0: {"session_type": 15, "time_offset": 0, "weather": 0, "rain_percentage": 0},
            1: {"session_type": 15, "time_offset": 1, "weather": 0, "rain_percentage": 0},
            2: {"session_type": 15, "time_offset": 2, "weather": 3, "rain_percentage": 60},
        },
    }
    inters_state._on_session(parse(PacketId.SESSION, pack_packet(PacketId.SESSION, inters_data)))
    assert inters_state.rain_box_lap == 0
    assert inters_state.rain_box_tyre == "inters"


def _car_status(compounds: tuple[int, int, int], session_time: float):
    packet = pack_packet(
        PacketId.CAR_STATUS,
        {
            "cars": {
                index: {
                    "actual_tyre_compound": compound,
                    "visual_tyre_compound": compound,
                }
                for index, compound in enumerate(compounds)
            }
        },
        session_time=session_time,
    )
    return parse(PacketId.CAR_STATUS, packet)


def _field_follow_predicate(state: SessionState) -> bool:
    settings = ConfigStore(isolated=True).current()
    rule = next(rule for rule in settings.rules if rule.id == "rain_follow_field")
    snapshot = replace(
        state.snapshot(0),
        phase="racing",
        laps_remaining=10,
        tyre_compound=17,
        weather_now=0,
        rain_onset_min=5,
    )
    namespace = make_namespace(
        snapshot,
        thresholds={"field_follow_min_cars": 2},
        mode={},
        staleness_age=lambda _: 0.0,
        staleness_limit=lambda _: 1.0,
    )
    return bool(Predicate(rule.when)(namespace))


def test_field_wet_switches_follow_recent_rival_compound_changes() -> None:
    state = SessionState(thresholds={"field_follow_min_cars": 2, "field_follow_window_laps": 3})
    state._player_idx = 0
    state.session_type = 15
    state.total_laps = 20
    state.lap_num = 4
    state._on_car_status(_car_status((17, 16, 16), 1.0), 1.0)
    state._on_car_status(_car_status((17, 8, 8), 2.0), 2.0)
    assert state.snapshot(2.0).field_wet_switches == 2
    assert state.snapshot(2.0).field_wet_tyre == "wets"
    assert _field_follow_predicate(state)

    mixed = SessionState(thresholds={"field_follow_min_cars": 2, "field_follow_window_laps": 3})
    mixed._player_idx = 0
    mixed.session_type = 15
    mixed.total_laps = 20
    mixed.lap_num = 4
    mixed._on_car_status(_car_status((17, 16, 16), 1.0), 1.0)
    mixed._on_car_status(_car_status((17, 7, 8), 2.0), 2.0)
    assert mixed.snapshot(2.0).field_wet_switches == 2
    assert mixed.snapshot(2.0).field_wet_tyre == "inters"

    one_rival = SessionState(thresholds={"field_follow_min_cars": 2, "field_follow_window_laps": 3})
    one_rival._player_idx = 0
    one_rival.session_type = 15
    one_rival.total_laps = 20
    one_rival.lap_num = 4
    one_rival._on_car_status(_car_status((17, 16, 18), 1.0), 1.0)
    one_rival._on_car_status(_car_status((17, 7, 18), 2.0), 2.0)
    assert one_rival.snapshot(2.0).field_wet_switches == 1
    assert one_rival.snapshot(2.0).field_wet_tyre == "inters"
    assert not _field_follow_predicate(one_rival)

    state.lap_num = 7
    state._on_car_status(_car_status((17, 8, 8), 3.0), 3.0)
    assert state.snapshot(3.0).field_wet_switches == 0
    assert state.snapshot(3.0).field_wet_tyre == ""
    assert not _field_follow_predicate(state)


def test_rain_rule_phrases_render_tyre_names() -> None:
    settings = ConfigStore(isolated=True).current()
    engine = RuleEngine(list(settings.rules), thresholds=settings.thresholds, mode={})
    snapshot = replace(
        SessionState().snapshot(0),
        field_wet_switches=2,
        field_wet_tyre="wets",
        rain_box_tyre="wets",
        rain_onset_min=5,
    )
    phrases = {
        "rain_follow_field": [
            "2 cars on wets. Box with them.",
            "Cars are switching to wets. Box this lap.",
        ],
        "rain_box_now": [
            "Box this lap for wets. Rain in 5 minutes.",
            "Box, box for wets. Rain due in 5 minutes.",
        ],
    }
    for rule_id, expected in phrases.items():
        rule = next(rule for rule in settings.rules if rule.id == rule_id)
        assert [
            engine.candidate_for(rule_id, snapshot, text=template).text
            for template in rule.say_pool()
        ] == expected
