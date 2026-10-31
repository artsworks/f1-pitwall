from __future__ import annotations

import pytest

from pitwall.strategy.weather import crossover_projection

from .race_synth import RaceSpec, race_stream, replay_race


def _fired(rows: list[dict]) -> set[str]:
    return {row.get("rule_id") for row in rows if row.get("outcome") == "fired"}


def _lap_extra(
    compounds: dict[int, int],
    lap_times: dict[int, int],
    *,
    invalid: tuple[int, ...] = (),
) -> dict[str, object]:
    return {
        "car_status": {
            "cars": {
                car: {
                    "actual_tyre_compound": compound,
                    "visual_tyre_compound": compound,
                    "tyres_age_laps": 5,
                }
                for car, compound in compounds.items()
            }
        },
        "lap_data": {
            "cars": {
                car: {
                    "last_lap_time_ms": lap_time,
                    **({"result_status": 1} if car in invalid else {}),
                }
                for car, lap_time in lap_times.items()
            }
        },
    }


def _lap_extras(
    times: list[dict[int, int]],
    compounds: dict[int, int],
    *,
    invalid_first: tuple[int, ...] = (),
) -> dict[int, dict[str, object]]:
    return {
        lap: _lap_extra(
            compounds,
            lap_times,
            invalid=invalid_first if lap == 1 else (),
        )
        for lap, lap_times in enumerate(times, start=1)
    }


def test_crossover_projection() -> None:
    assert crossover_projection(-4.0, -3.0, 0.3) == (3.0, 1.0, 3.0)
    assert crossover_projection(-2.0, -3.0, 0.3) is None
    assert crossover_projection(-3.1, -3.0, 0.3) is None
    assert crossover_projection(-1.0, 0.1, 0.3) is None


def test_default_race_stream_cache_key() -> None:
    default = race_stream(RaceSpec(laps=2))
    explicit_defaults = race_stream(RaceSpec(laps=2, weather=0, extra_by_lap={}))
    changed = race_stream(RaceSpec(laps=2, extra_by_lap={1: {"session": {"weather": 4}}}))
    assert default == explicit_defaults
    assert changed != default


@pytest.mark.replay
def test_rain_heads_up_uses_nearest_qualifying_forecast(tmp_path) -> None:
    calls, rows = replay_race(
        tmp_path,
        RaceSpec(laps=8, rain_lap=2, rain_percentages=(45, 50)),
    )
    fired = _fired(rows)
    assert "weather_rain_soon" in fired
    assert "weather_to_inter" not in fired
    call = next(call for call in calls if call.rule_id == "weather_rain_soon")
    assert "45 percent" in call.text


@pytest.mark.replay
def test_track_wetter_uses_clean_field_lap_pairs(tmp_path) -> None:
    calls, rows = replay_race(
        tmp_path,
        RaceSpec(
            laps=8,
            compound=16,
            weather=3,
            extra_by_lap=_lap_extras(
                [
                    {1: 90_000, 2: 90_000, 3: 90_000},
                    {1: 90_000, 2: 90_000, 3: 90_000},
                    {1: 92_000, 2: 92_000, 3: 92_000},
                    {1: 94_000, 2: 94_000, 3: 94_000},
                    {1: 96_000, 2: 96_000, 3: 96_000},
                ],
                {1: 16, 2: 16, 3: 16},
                invalid_first=(1, 2, 3),
            ),
        ),
    )
    fired = _fired(rows)
    assert "track_wetter" in fired
    assert "track_drier" not in fired
    assert "tyre_switch" not in fired
    call = next(call for call in calls if call.rule_id == "track_wetter")
    assert "2.0 seconds" in call.text


@pytest.mark.replay
def test_track_drier_calls_for_inters_when_field_times_fall(tmp_path) -> None:
    _, rows = replay_race(
        tmp_path,
        RaceSpec(
            laps=8,
            compound=7,
            weather=3,
            extra_by_lap=_lap_extras(
                [
                    {1: 96_000, 2: 96_000, 3: 96_000},
                    {1: 96_000, 2: 96_000, 3: 96_000},
                    {1: 94_000, 2: 94_000, 3: 94_000},
                    {1: 92_000, 2: 92_000, 3: 92_000},
                    {1: 90_000, 2: 90_000, 3: 90_000},
                ],
                {1: 7, 2: 7, 3: 7},
                invalid_first=(1, 2, 3),
            ),
        ),
    )
    fired = _fired(rows)
    assert "track_drier" in fired
    assert "track_wetter" not in fired


@pytest.mark.replay
def test_crossover_warning_precedes_tyre_switch(tmp_path) -> None:
    times = [
        {1: 0, 2: 0, 3: 0},
        {1: 90_000, 2: 94_060, 3: 94_060},
        {1: 90_000, 2: 93_120, 3: 93_120},
        {1: 90_000, 2: 92_180, 3: 92_180},
        {1: 90_000, 2: 91_240, 3: 91_240},
        {1: 90_000, 2: 90_300, 3: 90_300},
        {1: 90_000, 2: 89_360, 3: 89_360},
        {1: 90_000, 2: 88_420, 3: 88_420},
        {1: 90_000, 2: 87_480, 3: 87_480},
        {1: 90_000, 2: 86_540, 3: 86_540},
        {1: 90_000, 2: 85_600, 3: 85_600},
        {1: 90_000, 2: 84_660, 3: 84_660},
    ]
    calls, rows = replay_race(
        tmp_path,
        RaceSpec(
            laps=20,
            compound=7,
            weather=4,
            extra_by_lap=_lap_extras(times, {1: 7, 2: 16, 3: 16}),
        ),
    )
    fired = _fired(rows)
    assert "compound_crossover_close" in fired
    call_ids = [call.rule_id for call in calls]
    crossover_index = call_ids.index("compound_crossover_close")
    assert "slicks" in calls[crossover_index].text
    if "tyre_switch" in call_ids:
        assert crossover_index < call_ids.index("tyre_switch")


@pytest.mark.replay
def test_dry_default_race_has_no_new_weather_calls(tmp_path) -> None:
    _, rows = replay_race(tmp_path, RaceSpec())
    fired = _fired(rows)
    assert not fired.intersection(
        {"weather_rain_soon", "track_wetter", "track_drier", "compound_crossover_close"}
    )
