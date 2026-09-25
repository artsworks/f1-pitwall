from __future__ import annotations

import math
from types import SimpleNamespace

from pitwall.state.quali import (
    abort_advice,
    projected_lap_ms,
    quali_cutoff_ms,
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


def test_quali_cutoff() -> None:
    elim = {5: 5, 6: 5, 7: 0}
    field = [90_000 + i * 100 for i in range(20)]  # 90.0 .. 91.9 sorted
    # Q1 with 20 cars: cut-off position 15 -> 15th best (index 14) = 91.400
    assert quali_cutoff_ms(field, 20, 5, elim) == 91_400
    # Q3: nobody eliminated -> 0
    assert quali_cutoff_ms(field, 10, 7, elim) == 0
    # too few laps set -> everyone through so far
    assert quali_cutoff_ms(field[:10], 20, 5, elim) == 0
    # sprint shootout 10 maps to Q1
    assert quali_cutoff_ms(field, 20, 10, elim) == 91_400


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
