from __future__ import annotations

from pitwall.engine import build_engine
from pitwall.store.db import LapRow


def _lap(lap_num: int, fuel_kg: float, tyre_inner_c: float) -> LapRow:
    return LapRow(
        id=lap_num,
        session_uid=1,
        car_idx=0,
        lap_num=lap_num,
        lap_time_ms=90_000 + lap_num * 100,
        s1_ms=0,
        s2_ms=0,
        compound=18,
        tyre_age_laps=lap_num,
        fuel_remaining_laps=20.0,
        valid=1,
        invalid_reasons=(),
        wear_pct=0.0,
        fuel_kg=fuel_kg,
        ers_deployed_j=0.0,
        sc_status=0,
        weather=0,
        tyre_inner_c=tyre_inner_c,
    )


def test_fuel_forecast_reference_uses_fit_laps_fallback(tmp_path, monkeypatch) -> None:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    monkeypatch.setattr(engine, "_thermal_hi_c", lambda *args: 90.0)
    tail = [
        _lap(1, 30.0, 100.0),
        _lap(2, 29.0, 80.0),
        _lap(3, 28.0, 100.0),
        _lap(4, 27.0, 100.0),
        _lap(5, 26.0, 100.0),
    ]

    engine._fit_current_stint(tail, 7, engine.store.current(), None)

    assert engine._stint_fuel_ref == 30.0
