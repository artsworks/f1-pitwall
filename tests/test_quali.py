from __future__ import annotations

import math
from types import SimpleNamespace

from pitwall.state.quali import (
    abort_advice,
    projected_lap_ms,
    quali_cutoff_ms,
    quali_cutoff_pos,
    quali_margin_ms,
    quali_through,
    release_window,
)


def _car(lap_distance: float, **kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "lap_distance": lap_distance,
        "driver_status": 1,  # flying
        "pit_status": 0,
        "result_status": 2,
    }
    base.update(kw)
    return SimpleNamespace(**base)


L = 5000.0


def test_release_empty_track_clean() -> None:
    rw = release_window(
        [_car(0.0, driver_status=0)],  # player entry is skipped by idx
        0,
        L,
        0.0,
        120.0,
        [0],
        95.0,
        4.0,
    )
    assert rw.clean
    assert rw.cars_on_track == 0
    assert math.isinf(rw.gap_ahead_s)
    assert rw.wait_s == 0.0


def test_release_car_behind_exit_not_clean() -> None:
    # Rival 100 m before pit exit on a 5 km track at ~95 s pace. out_lap_s=0
    # keeps it behind the exit at the player's arrival.
    cars = [_car(0.0, driver_status=0), _car(4900.0)]
    rw = release_window(cars, 0, L, 0.0, 0.0, [0, 95_000], 95.0, 4.0)
    assert not rw.clean
    assert rw.cars_on_track == 1
    assert rw.gap_behind_s < 4.0
    assert 0.0 < rw.wait_s <= 60.0


def test_release_car_far_ahead_clean() -> None:
    cars = [_car(0.0, driver_status=0), _car(2500.0)]
    rw = release_window(cars, 0, L, 0.0, 120.0, [0, 95_000], 95.0, 4.0)
    assert rw.clean
    assert rw.gap_ahead_s > 4.0
    assert rw.gap_behind_s > 4.0
    assert rw.wait_s == 0.0


def test_release_skips_garage_and_finished() -> None:
    cars = [
        _car(0.0, driver_status=0),  # player
        _car(4900.0, driver_status=0),  # in garage: ignored
        _car(4900.0, pit_status=1),  # pitting: ignored
        _car(4900.0, result_status=1),  # inactive: ignored
    ]
    rw = release_window(cars, 0, L, 0.0, 120.0, [0] * 4, 95.0, 4.0)
    assert rw.cars_on_track == 0
    assert rw.clean


def test_projected_lap_ms() -> None:
    bests = (30_000, 30_000, 30_000)
    # sector 0: all bests
    assert projected_lap_ms(0, 15_000, 0, 0, *bests) == 90_000
    # sector 1: actual s1 + best s2/s3
    assert projected_lap_ms(1, 32_000, 32_000, 0, *bests) == 92_000
    # sector 2: actuals + best s3 (S3 elapsed 13s < 30s best -> no overrun)
    assert projected_lap_ms(2, 75_000, 32_000, 30_000, *bests) == 92_000
    # S3 elapsed 38s vs 30s best -> 8s overrun
    assert projected_lap_ms(2, 100_000, 32_000, 30_000, *bests) == 100_000
    # missing best -> 0
    assert projected_lap_ms(1, 32_000, 32_000, 0, 30_000, 0, 30_000) == 0
    # a spin-lap S3 best is capped by the cut-off car's S3
    slow = (29_569, 39_022, 33_987)
    cut = (29_900, 37_900, 26_780)
    assert projected_lap_ms(1, 29_461, 29_461, 0, *slow) == 102_470
    assert projected_lap_ms(1, 29_461, 29_461, 0, *slow, ref_sectors=cut) == 94_141


def test_quali_cutoff() -> None:
    field = [90_000 + i * 100 for i in range(20)]  # 90.0 .. 91.9 sorted
    # Q1 advances 15 cars, so the cut-off is the 15th best (index 14).
    assert quali_cutoff_ms(field, 0, 20, 5, {5: 15, 6: 10, 7: 0}) == 91_400
    # Q3: nobody eliminated -> 0
    assert quali_cutoff_ms(field, 0, 20, 7, {5: 15, 6: 10, 7: 0}) == 0
    # Untimed cars count as ahead of the timed field.
    assert quali_cutoff_ms(field[:10], 0, 20, 5, {5: 15, 6: 10, 7: 0}) == field[4]
    assert quali_cutoff_ms(field[:4], 0, 20, 5, {5: 15, 6: 10, 7: 0}) == 0
    # sprint shootout 10 maps to Q1
    assert quali_cutoff_ms(field, 0, 20, 10, {5: 15, 6: 10, 7: 0}) == 91_400


def test_quali_cutoff_counts_untimed_cars() -> None:
    advance = {5: 15, 6: 10, 7: 0}
    times = [80_000 + i * 100 for i in range(16)]
    assert quali_cutoff_ms(times[:10], 0, 16, 6, advance) == times[3]
    assert quali_cutoff_ms(times[:14], 0, 16, 6, advance) == times[7]
    assert quali_cutoff_ms(times, 0, 16, 6, advance) == times[9]
    assert quali_cutoff_ms(times[:4], 0, 16, 6, advance) == 0


def test_quali_cutoff_does_not_count_untimed_player_as_rival() -> None:
    advance = {5: 15, 6: 10, 7: 0}
    times = [90_000 + i * 100 for i in range(19)]
    field = [0, *times]
    assert quali_cutoff_ms(field, 0, 20, 5, advance) == times[14]


def test_quali_margin_counts_untimed_cars() -> None:
    advance = {5: 15, 6: 10, 7: 0}
    field = [
        81_000,
        79_000,
        79_500,
        80_000,
        80_500,
        81_500,
        82_000,
        82_500,
        82_800,
        82_900,
    ]
    field.extend([0] * 6)
    assert quali_margin_ms(field, 0, 16, 6, advance) == (-500, "cut")

    complete_field = field[:10] + [83_000, 83_500, 84_000, 84_500, 85_000, 85_500]
    assert quali_margin_ms(complete_field, 0, 16, 6, advance) == (2_000, "cut")
    incomplete_field = [81_000, 79_000, 79_500, 80_000, 80_500] + [0] * 11
    assert quali_margin_ms(incomplete_field, 0, 16, 6, advance) == (0, "")

    # Q3 cannot estimate pole margin while an active car has no time.
    assert quali_margin_ms([80_000, 81_000, 0], 0, 3, 7, advance) == (0, "")


def test_quali_cutoff_pos() -> None:
    advance = {5: 15, 6: 10, 7: 0}
    assert quali_cutoff_pos(5, advance) == 15
    assert quali_cutoff_pos(6, advance) == 10
    assert quali_cutoff_pos(7, advance) == 0
    assert quali_cutoff_pos(11, advance) == 10
    assert quali_cutoff_pos(8, advance) == 0


def test_quali_through_regression_for_incomplete_q2_field() -> None:
    field = [
        80_062,
        80_261,
        80_581,
        80_837,
        80_875,
        80_972,
        81_144,
        81_148,
        81_416,
        81_813,
        82_477,
        0,
        0,
        0,
        0,
        0,
    ]
    assert not quali_through(
        field,
        9,
        10,
        16,
        6,
        567,
        advance={5: 15, 6: 10, 7: 0},
        safe_margin_ms=300,
        open_margin_ms=500,
        open_window_s=300,
    )


def test_quali_through_rejects_position_beyond_cut() -> None:
    field = [79_400, 79_000, 79_100, 79_200, 79_300]
    field.extend([79_500 + i * 100 for i in range(11)])
    assert not quali_through(
        field,
        0,
        11,
        16,
        6,
        0,
        advance={5: 15, 6: 10, 7: 0},
        safe_margin_ms=300,
        open_margin_ms=500,
        open_window_s=300,
    )


def test_quali_through_treats_cars_without_times_as_ahead() -> None:
    field = [79_000 + i * 100 for i in range(8)] + [80_000, 82_000, 83_000, 84_000]
    field.extend([0] * 4)
    assert not quali_through(
        field,
        8,
        9,
        16,
        6,
        0,
        advance={5: 15, 6: 10, 7: 0},
        safe_margin_ms=300,
        open_margin_ms=500,
        open_window_s=300,
    )
    complete_field = field.copy()
    complete_field[12:] = [85_000, 86_000, 87_000, 88_000]
    assert quali_through(
        complete_field,
        8,
        9,
        16,
        6,
        0,
        advance={5: 15, 6: 10, 7: 0},
        safe_margin_ms=300,
        open_margin_ms=500,
        open_window_s=300,
    )


def test_quali_through_margin_shrinks_near_flag() -> None:
    field = [79_000 + i * 100 for i in range(8)] + [80_900, 81_500]
    field.extend([82_000 + i * 100 for i in range(6)])
    kwargs = {
        "advance": {5: 15, 6: 10, 7: 0},
        "safe_margin_ms": 300,
        "open_margin_ms": 500,
        "open_window_s": 300,
    }
    assert not quali_through(field, 8, 9, 16, 6, 600, **kwargs)
    assert quali_through(field, 8, 9, 16, 6, 0, **kwargs)


def test_abort_advice() -> None:
    # Player already safely through -> never advise
    a = abort_advice(92_000, 91_000, 89_000, 2, fresh_sets_current=2, ers_store_pct=30.0)
    assert not a.advised and a.reason == "through"
    # Projected 0.9 over a 400 ms margin -> advise
    a = abort_advice(91_900, 91_000, 0, 2, fresh_sets_current=2, ers_store_pct=30.0)
    assert a.advised and a.deficit_ms == 900
    # No fresh sets: margin x3 -> same deficit not advised
    a = abort_advice(91_900, 91_000, 0, 2, fresh_sets_current=0, ers_store_pct=30.0)
    assert not a.advised and a.reason == "no_fresh_sets"
    # ERS banked: margin x0.7 (280 ms) -> advised at 400 ms over
    a = abort_advice(91_400, 91_000, 0, 2, fresh_sets_current=2, ers_store_pct=70.0)
    assert a.advised
    # No cut-off yet: no deficit reported
    a = abort_advice(88_979, 0, 88_981, 0, fresh_sets_current=2, ers_store_pct=30.0)
    assert not a.advised and a.deficit_ms == 0 and a.reason == "no_cutoff"
    # Sector 0 never advises
    a = abort_advice(95_000, 91_000, 0, 0, fresh_sets_current=2, ers_store_pct=30.0)
    assert not a.advised


def test_abort_advice_uses_untimed_adjusted_cutoff() -> None:
    advance = {5: 15, 6: 10, 7: 0}
    field = [79_000, 79_500, 80_000, 80_500, 80_800, 81_000, 81_200, 81_400, 82_000, 83_000]
    cutoff = quali_cutoff_ms(field, 5, 16, 6, advance)
    old_cutoff = sorted(field)[9]
    assert cutoff == sorted(field)[3]
    assert abort_advice(85_000, cutoff, 81_000, 2, 1, 50.0).reason != "through"
    assert abort_advice(85_000, old_cutoff, 81_000, 2, 1, 50.0).reason == "through"
