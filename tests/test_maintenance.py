"""Automatic learned-state maintenance and race-distance scoped priors."""

from __future__ import annotations

import io

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine
from pitwall.maintenance import maintain
from pitwall.model.deg import DegFit, corner_wear_life, fit_is_clean, planning_fit, scoped
from pitwall.store.db import Database

TH = ConfigStore().current().thresholds


def _session(db: Database, uid: int, *, track: int, stype: int, laps: int) -> None:
    db.upsert_session(uid, track_id=track, session_type=stype, started_at=float(uid))
    db.set_session_total_laps(uid, laps)


def test_worst_corner_bounds_life() -> None:
    start = (0.0, 0.0, 0.0, 0.0)
    wear = (10.0, 10.0, 20.0, 10.0)
    life = corner_wear_life(wear, start, 5, wear_cliff_pct=70, default_rate_pct=2.5)
    assert life == (70 - 20) / 4.0
    even = corner_wear_life((20.0,) * 4, start, 5, wear_cliff_pct=70, default_rate_pct=2.5)
    assert even == life
    fast = corner_wear_life(
        (5.0, 5.0, 12.0, 5.0), start, 2, wear_cliff_pct=70, default_rate_pct=2.5
    )
    assert fast < corner_wear_life((5.0,) * 4, start, 2, wear_cliff_pct=70, default_rate_pct=2.5)


def test_corner_life_uses_default_rate_before_a_lap() -> None:
    life = corner_wear_life((1.0,) * 4, (0.0,) * 4, 0.5, wear_cliff_pct=70, default_rate_pct=2.0)
    assert life == (70 - 1.0) / 2.0


def test_planning_fit_shrinks_noisy_slope() -> None:
    prior = DegFit(90_000.0, 80.0, 30.0, 0, 0.0, 0.2, "prior")
    noisy = DegFit(90_000.0, 600.0, 30.0, 4, 900.0, 0.2, "fit")
    clean = DegFit(90_000.0, 600.0, 30.0, 4, 0.0, 0.4, "fit")
    assert planning_fit(noisy, prior, 800).deg_ms_per_lap == 80.0
    assert planning_fit(clean, prior, 800).deg_ms_per_lap == 600.0
    half = DegFit(90_000.0, 600.0, 30.0, 4, 400.0, 0.4, "fit")
    assert planning_fit(half, prior, 800).deg_ms_per_lap == 0.5 * 600 + 0.5 * 80
    assert planning_fit(prior, prior, 800) is prior


def test_clean_fit_gate() -> None:
    kw = {
        "deg_max_ms_per_lap": 600,
        "deg_rmse_bad_ms": 800,
        "base_min_ms": 40_000,
        "base_max_ms": 200_000,
    }
    assert fit_is_clean(DegFit(90_000.0, 80.0, 30.0, 8, 100.0, 0.9, "fit"), **kw)
    assert not fit_is_clean(DegFit(90_000.0, 600.0, 30.0, 8, 100.0, 0.9, "fit"), **kw)
    assert not fit_is_clean(DegFit(90_000.0, 0.0, 30.0, 8, 100.0, 0.9, "fit"), **kw)
    assert not fit_is_clean(DegFit(20_000.0, 80.0, 30.0, 8, 100.0, 0.9, "fit"), **kw)


def test_race_distance_scoping() -> None:
    assert scoped("deg_ms_per_lap", 0) == "deg_ms_per_lap"
    assert scoped("deg_ms_per_lap", 13) != scoped("deg_ms_per_lap", 52)


def test_maintain_quarantines_rebuilds_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "m.sqlite"
    db = Database(path)
    _session(db, 1, track=7, stype=15, laps=13)
    _session(db, 2, track=7, stype=15, laps=52)
    _session(db, 3, track=7, stype=1, laps=0)
    db.upsert_stint(1, 0, 17, 1, 8, DegFit(90_000.0, 250.0, 30.0, 8, 100.0, 0.9, "fit"))
    db.upsert_stint(2, 0, 17, 1, 20, DegFit(90_000.0, 90.0, 30.0, 20, 100.0, 0.9, "fit"))
    db.upsert_stint(3, 0, 17, 1, 6, DegFit(90_000.0, 120.0, 30.0, 6, 100.0, 0.9, "fit"))
    db.upsert_stint(2, 0, 19, 21, 27, DegFit(55_520.0, 600.0, 0.0, 7, 7_992.0, 0.2, "fit"))
    db.fold_param(7, 19, "deg_ms_per_lap", 600.0, weight=7)
    db.fold_param(-1, 18, "base_ms", 95_000.0, weight=3)
    db.fold_param(7, 0, "fuel_kg_per_lap", 1.4, weight=5)

    first = maintain(db, TH)
    assert first.rebuilt == 9
    assert any("track -1" in q for q in first.quarantined)
    assert db.get_param(7, 19, "deg_ms_per_lap") is None
    assert db.get_param(7, 17, scoped("deg_ms_per_lap", 13)).value == 250.0  # type: ignore[union-attr]
    assert db.get_param(7, 17, scoped("deg_ms_per_lap", 52)).value == 90.0  # type: ignore[union-attr]
    assert db.get_param(7, 17, "deg_ms_per_lap").value == 120.0  # type: ignore[union-attr]
    assert db.get_param(7, 0, "fuel_kg_per_lap").value == 1.4  # type: ignore[union-attr]
    reasons = {(q["track_id"], q["name"], q["reason"]) for q in db.quarantined_params()}
    assert (7, "deg_ms_per_lap", "deg_clamped") in reasons
    assert (-1, "base_ms", "unknown_track") in reasons

    before = [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()]
    db.close()
    db = Database(path)
    again = maintain(db, TH)
    assert again.rebuilt == 0 and not again.quarantined and again.graded == 0
    assert [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()] == before


def test_maintain_on_empty_db(tmp_path) -> None:
    db = Database(tmp_path / "empty.sqlite")
    assert maintain(db, TH).summary() == "learned state clean"


def test_race_priors_do_not_mix_race_distances() -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    db.fold_param(7, 17, scoped("deg_ms_per_lap", 13), 250.0, weight=20)
    db.fold_param(7, 17, "deg_ms_per_lap", 120.0, weight=20)
    state = engine.state
    state.session_type = 15
    state.total_laps = 13
    assert engine._learned_name(7, 17, "deg_ms_per_lap") == scoped("deg_ms_per_lap", 13)  # noqa: SLF001
    state.total_laps = 52
    assert engine._learned_name(7, 17, "deg_ms_per_lap") == "deg_ms_per_lap"  # noqa: SLF001
    engine._fold_fit(7, 17, DegFit(90_000.0, 90.0, 30.0, 20, 100.0, 0.9, "fit"))  # noqa: SLF001
    assert db.get_param(7, 17, scoped("deg_ms_per_lap", 52)).value == 90.0  # type: ignore[union-attr]
    assert db.get_param(7, 17, scoped("deg_ms_per_lap", 13)).value == 250.0  # type: ignore[union-attr]
    assert engine._learned_name(7, 17, "deg_ms_per_lap") == scoped("deg_ms_per_lap", 52)  # noqa: SLF001
    state.session_type = 1
    assert engine._learned_name(7, 17, "deg_ms_per_lap") == "deg_ms_per_lap"  # noqa: SLF001
