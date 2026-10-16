from __future__ import annotations

import dataclasses

from pitwall.engine import _hold_cover_plan
from pitwall.state.session import Snapshot
from pitwall.strategy.pitwindow import NO_PLAN


def test_held_cover_plan_survives_until_next_racing_lap() -> None:
    cover = dataclasses.replace(NO_PLAN, plan="cover", lap=5, rival_idx=2, rival_name="PIASTRI")
    fresh = dataclasses.replace(NO_PLAN, plan="box_in_n")
    held = (cover, 5)

    for lap_num in (5, 6):
        selected, next_held = _hold_cover_plan(
            held,
            fresh,
            Snapshot(now=float(lap_num), phase="racing", lap_num=lap_num, laps_remaining=10),
            2,
        )
        assert selected == cover
        assert next_held == held

    after_window, cleared = _hold_cover_plan(
        held,
        fresh,
        Snapshot(now=7.0, phase="racing", lap_num=7, laps_remaining=10),
        2,
    )
    assert after_window == fresh
    assert cleared is None

    after_phase_change, cleared = _hold_cover_plan(
        held,
        fresh,
        Snapshot(now=6.0, phase="sc", lap_num=6, laps_remaining=10),
        2,
    )
    assert after_phase_change == fresh
    assert cleared is None
