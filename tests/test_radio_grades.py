from pathlib import Path

import pytest

from pitwall.audio.grades import load_workbook, replay
from pitwall.config.loader import ConfigStore

WORKBOOK = Path(__file__).parents[1] / "scenarios" / "radio-priority-grades.yaml"
SCENARIOS = load_workbook(WORKBOOK)

# Owner grades the dispatcher does not meet yet. Remove an id once policy changes make it pass.
KNOWN_MISMATCHES: dict[str, str] = {
    "g004": "conflict resolution",
    "g006": "preemption",
    "g007": "preemption",
    "g011": "preemption",
    "g012": "preemption",
    "g017": "digest merging",
    "g018": "focus suppression",
    "g020": "preemption",
    "g021": "preemption",
    "g022": "preemption",
    "g024": "digest merging",
    "g026": "cooldown group",
    "g038": "conflict resolution",
    "g040": "focus suppression",
    "g041": "preemption",
    "g044": "preemption",
    "g053": "conflict resolution",
    "g055": "preemption",
    "g056": "preemption",
    "g057": "conflict resolution",
    "g060": "conflict resolution",
    "g067": "preemption",
    "g071": "digest merging",
    "g072": "conflict resolution",
    "g074": "digest merging",
    "g080": "rule gating",
    "g082": "preemption",
    "g085": "preemption",
    "g087": "focus suppression",
    "g090": "queue flush",
    "g092": "preemption",
    "g093": "conflict resolution",
    "g096": "conflict resolution",
    "g099": "conflict resolution",
    "g100": "digest merging",
}


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            scenario,
            id=scenario.id,
            marks=pytest.mark.xfail(strict=True, reason=KNOWN_MISMATCHES[scenario.id]),
        )
        if scenario.id in KNOWN_MISMATCHES
        else pytest.param(scenario, id=scenario.id)
        for scenario in SCENARIOS
    ],
)
def test_radio_grade_scenario(scenario) -> None:
    result = replay(scenario, ConfigStore(isolated=True).current())
    assert not result.mismatches, result.mismatches


def test_known_mismatch_ids_are_workbook_scenarios() -> None:
    scenario_ids = {scenario.id for scenario in SCENARIOS}
    assert set(KNOWN_MISMATCHES) <= scenario_ids


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
