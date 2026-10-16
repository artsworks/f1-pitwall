from __future__ import annotations

import ast
from pathlib import Path

from pitwall.config.loader import ConfigStore

from .test_rule_replay import NOT_REPLAYABLE

# These rules currently have only unit tests. Remove each id when replay coverage lands.
KNOWN_GAPS: frozenset[str] = frozenset(
    {
        "battle_catching",
        "battle_patience",
        "boost_left_on",
        "contact_check",
        "contact_check_teammate",
        "cool_car_behind",
        "cool_extend",
        "cool_hot_mode",
        "cool_mistakes",
        "cool_push_check",
        "cool_recharge",
        "cool_recharge_check",
        "cool_status",
        "cool_vs_pole",
        "cool_vs_pole_split",
        "cover_box",
        "drs_fault",
        "ers_fault",
        "fastest_lap_mine",
        "fastest_lap_taken",
        "floor_damage",
        "fuel_marginal",
        "fuel_short",
        "gearbox_damage",
        "grid_penalty",
        "lap_deleted",
        "lap_deleted_last",
        "last_lap_attack",
        "lockup_front",
        "lockup_front_same_spot",
        "lockup_rear",
        "next_lap_invalid",
        "off_track_recovered",  # replay scenario unresolved: recovery timing did not trigger
        "overcut_defend",
        "overcut_stay_out",  # replay scenario unresolved: rival pit did not yield enough gain
        "penalty_cost",
        "penalty_covered",
        "pit_exit_traffic",
        "plan_off",  # replay scenario unresolved: the post-stop plan switched directly to no-stop
        "plan_announce_no_stop",
        "plan_box",
        "plan_box_safe",
        "plan_cool",
        "plan_push",
        "plan_push_now",
        "plan_switch",
        "plan_switch_no_stop",
        "plan_target_lap",
        "practice_lap_invalid",
        "prep_car_behind",
        "prep_clear",
        "prep_dirty_air",
        "prep_tow",
        "prep_traffic_ahead",
        "quali_safe_cut",
        "quali_safe_pole",
        "quali_through",  # replay scenario unresolved: field history did not trigger a decision
        "saved_moment",
        "setup_bias",
        "setup_on_throttle",  # replay scenario unresolved: high slip did not yield advice
        "setup_stop_wing",  # replay scenario unresolved: stop setup did not yield advice
        "serve_penalty",  # replay scenario unresolved: pit timing did not trigger advice
        "slow_car_ahead",
        "slow_car_crawling",
        "spun_rejoin",
        "spun_rejoin_traffic",
        "teammate_fight",
        "tyre_life",
        "tyre_pressure_advice",
        "tyre_pressure_ok",
        "yellow_ahead",
        "yellow_behind",
    }
)


def test_every_rule_has_replay_coverage_or_known_gap() -> None:
    rules_dir = Path(__file__).resolve().parents[1] / "src/pitwall/config/defaults/rules"
    rule_ids = {rule.id for rule in ConfigStore(rules_dir=rules_dir).current().rules}

    replay_literals: set[str] = set()
    for path in Path(__file__).parent.glob("test_*.py"):
        if path == Path(__file__):
            continue
        source = path.read_text()
        if "pytest.mark.replay" not in source:
            continue
        tree = ast.parse(source)
        replay_literals.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )

    covered = (rule_ids & replay_literals) | set(NOT_REPLAYABLE)
    assert rule_ids - covered == KNOWN_GAPS
