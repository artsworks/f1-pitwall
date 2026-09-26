from __future__ import annotations

from pitwall.protocol.layouts import Corners
from pitwall.state.pressure import RunTemps, pressure_advice, pressure_text
from pitwall.state.quali import quali_margin_ms


def test_run_temps_time_weighted_and_flying_only() -> None:
    r = RunTemps()
    hot = Corners(100.0, 100.0, 100.0, 100.0)
    cold = Corners(50.0, 50.0, 50.0, 50.0)
    r.update(0.0, cold, active=False)
    r.update(0.1, cold, active=False)  # not flying: ignored
    for i in range(2, 12):
        r.update(i / 10, hot, active=True)
    r.update(5.0, cold, active=True)  # gap > max_dt (pause/rewind): ignored
    assert abs(r.seconds - 1.0) < 1e-9
    assert abs(r.mean().fl - 100.0) < 1e-9
    r.reset()
    assert r.seconds == 0 and r.mean().fl == 0


def test_pressure_advice_sizes_and_direction() -> None:
    avg = Corners(rl=95.0, rr=84.0, fl=106.0, fr=115.0)
    psi = Corners(21.0, 21.0, 23.0, 23.0)
    calls = pressure_advice(avg, psi, 88.0, 102.0)
    by = {c.corner: c for c in calls}
    assert "rl" not in by  # inside the window
    assert by["fl"].size == "small" and by["fl"].delta_psi == -0.2 and by["fl"].target_psi == 22.8
    assert by["fr"].size == "large" and by["fr"].delta_psi == -0.8
    assert by["rr"].size == "small" and by["rr"].delta_psi == 0.2
    assert pressure_text(calls) == (
        "drop the front left 0.2, drop the front right 0.8, raise the rear right 0.2"
    )
    flipped = pressure_advice(avg, Corners(0, 0, 0, 0), 88.0, 102.0, hot_sign=1.0)
    assert {c.corner: c.delta_psi for c in flipped}["fr"] == 0.8
    assert all(c.target_psi == 0.0 for c in flipped)


def test_pressure_text_groups_axles_and_clamps_to_setup_range() -> None:
    # Game direction: cold tyres want less pressure; fronts already at the minimum.
    avg = Corners(rl=82.0, rr=82.0, fl=80.0, fr=80.0)
    psi = Corners(rl=21.0, rr=21.0, fl=22.5, fr=22.5)
    calls = pressure_advice(
        avg, psi, 88.0, 102.0, hot_sign=1.0, front_range=(22.5, 29.5), rear_range=(20.5, 26.5)
    )
    by = {c.corner: c for c in calls}
    assert by["fl"].limited and by["fl"].delta_psi == 0 and by["fl"].wanted_psi == -0.4
    assert by["rl"].delta_psi == -0.4 and by["rl"].target_psi == 20.6
    assert pressure_text(calls) == "the fronts are already at the minimum, drop the rears 0.4"
    same = pressure_advice(Corners(110.0, 110.0, 110.0, 110.0), psi, 88.0, 102.0, hot_sign=1.0)
    assert pressure_text(same) == "raise all four 0.4"


def test_quali_margin_cut_and_pole() -> None:
    best = [0] * 22
    best[0] = 80_000  # player
    for i in range(1, 22):
        best[i] = 81_000 + i * 100
    # Q1: 22 cars, 5 out -> 17 safe; the 17th fastest other car is 82_700
    assert quali_margin_ms(best, 0, 22, 5, {5: 5, 6: 5, 7: 0}) == (2_700, "cut")
    # Q3: gap to P2
    assert quali_margin_ms(best, 0, 22, 7, {5: 5, 6: 5, 7: 0}) == (1_100, "pole")
    # Behind on pole -> negative margin
    assert quali_margin_ms(best, 5, 22, 7, {5: 5, 6: 5, 7: 0})[0] < 0
    # Not enough times yet
    assert quali_margin_ms([80_000, 81_000], 0, 22, 5, {5: 5}) == (0, "")
    assert quali_margin_ms([0, 81_000], 0, 22, 7, {7: 0}) == (0, "")
