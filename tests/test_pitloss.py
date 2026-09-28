"""Pit loss: measured loss, and current_pit_loss resolution order."""

from __future__ import annotations

from pitwall.config.models import TrackOverlay
from pitwall.model.pitloss import current_pit_loss, measure, ref_pace_ms
from pitwall.store.db import Database, LapRow


def _lap(num: int, time_ms: int, *, valid: int = 1, sc: int = 0) -> LapRow:
    return LapRow(
        id=num,
        session_uid=1,
        car_idx=0,
        lap_num=num,
        lap_time_ms=time_ms,
        s1_ms=0,
        s2_ms=0,
        compound=18,
        tyre_age_laps=num,
        fuel_remaining_laps=10.0,
        valid=valid,
        invalid_reasons=(),
        wear_pct=0.0,
        fuel_kg=0.0,
        ers_deployed_j=0.0,
        sc_status=sc,
        weather=0,
    )


def test_measure() -> None:
    pit = measure(
        _lap(8, 106_000), _lap(9, 98_000), lane_ms=19_500, ref_pace_ms=90_000, neutralised=0
    )
    assert pit.loss_ms == (16_000 + 8_000)
    assert pit.lane_ms == 19_500 and pit.ref_pace_ms == 90_000


def test_ref_pace_median_of_last_3_valid() -> None:
    laps = [_lap(i, 90_000 + i * 10) for i in range(1, 6)]
    laps[2] = _lap(3, 90_030, valid=0)  # lap 3 excluded
    # before lap 6: valid = 1,2,4,5 -> last 3 = 2,4,5 -> median = lap 4
    assert ref_pace_ms(laps, 6) == 90_040
    assert ref_pace_ms([], 6) == 0


def test_current_pit_loss_resolution_order() -> None:
    th = {"prior_min_weight": 2, "pit_loss_default_s": 22.0}
    overlay = TrackOverlay(track_id=7, pit_loss_s={"green": 21.5})

    # default when nothing known
    db = Database(":memory:")
    db.upsert_session(1, track_id=7)
    p = current_pit_loss(db, 1, 7, 0, None, th)
    assert p.source == "default" and p.value == 22_000.0

    # overlay beats default
    p = current_pit_loss(db, 1, 7, 0, overlay, th)
    assert p.source == "overlay" and p.value == 21_500.0

    # learned param (weight >= min) beats overlay
    db.fold_param(7, 0, "pit_loss_green_ms", 20_000.0, weight=3.0)
    p = current_pit_loss(db, 1, 7, 0, overlay, th)
    assert p.source == "learned" and p.value == 20_000.0

    # this-session measured events beat learned
    db.insert_pit_event(1, 0, 9, 19_000, 0, 0, 0, 0, 90_000)
    p = current_pit_loss(db, 1, 7, 0, overlay, th)
    assert p.source == "session" and p.value == 19_000.0

    # neutralised=1 (SC) uses its own param/overlay key/defaults
    p = current_pit_loss(db, 1, 7, 1, overlay, {"pit_loss_sc_default_s": 8.0})
    assert p.source == "default" and p.value == 8_000.0
    db.fold_param(7, 0, "pit_loss_sc_ms", 7_500.0, weight=3.0)
    p = current_pit_loss(db, 1, 7, 1, overlay, {"pit_loss_sc_default_s": 8.0})
    assert p.source == "learned" and p.value == 7_500.0
