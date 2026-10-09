from pathlib import Path

import pytest

from pitwall.audio.grades import load_workbook, replay
from pitwall.config.loader import ConfigStore

WORKBOOK = Path(__file__).parents[1] / "scenarios" / "radio-priority-grades.yaml"
SCENARIOS = load_workbook(WORKBOOK)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_radio_grade_scenario(scenario) -> None:
    result = replay(scenario, ConfigStore(isolated=True).current())
    assert not result.mismatches, result.mismatches


def test_rotation_is_seeded_and_varies_for_r14() -> None:
    scenario = next(item for item in SCENARIOS if item.id == "r14")
    settings = ConfigStore(isolated=True).current()
    winners = set()
    for seed in range(1, 21):
        first = replay(scenario, settings, session_uid=seed)
        second = replay(scenario, settings, session_uid=seed)
        assert first.actions == second.actions
        winners.update(
            call_id for call_id in ("lost", "attack") if first.actions.get(call_id) != "drop"
        )

    assert winners == {"lost", "attack"}
