from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from pitwall.cli import main
from pitwall.learning.bench import (
    ScenarioCheck,
    _scenario_ops,
    build_recording_index,
    build_scorecard,
    compare,
    evaluate_checks,
    find_source,
    keep_missing,
    load_scenarios,
    parse_scenario,
    trend,
)
from pitwall.net.derive import ops_from_options

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream

pytestmark = pytest.mark.slow


def _scenario_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "case",
        "title": "Test case",
        "status": "guard",
        "source": {
            "session_uid": "0x1234",
            "sha256": "a" * 64,
        },
        "expect": {"fire": [{"rule": "sc_deployed", "laps": [3, 4]}]},
    }
    data.update(overrides)
    return data


def _write_scenario(directory: Path, data: dict[str, Any]) -> Path:
    path = directory / f"{data['id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _check(
    kind: str = "fire",
    rules: tuple[str, ...] = ("sc_deployed",),
    laps: tuple[int, int] = (3, 4),
    ok: bool = True,
) -> dict[str, Any]:
    return {"type": kind, "rules": list(rules), "laps": list(laps), "ok": ok, "found": []}


def _scenario(
    result: str,
    *,
    status: str = "target",
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "title": "Scenario",
        "status": status,
        "kind": "real",
        "result": result,
        "reason": "",
        "checks": checks or [],
    }


def _card(
    scenarios: dict[str, Any] | None = None, metrics: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "scenarios": scenarios or {},
        "metrics": metrics or {},
    }


def _patch_bench_run(monkeypatch, results: dict[str, str] | None = None) -> None:
    import pitwall.learning.bench as bench_module

    scenario_results = results or {}

    def fake_run_scenario(scenario, source, settings, *, rules_dir, skip_reason):
        result = scenario_results.get(scenario.id, "pass")
        checks = [
            {
                "type": check.type,
                "rules": list(check.rules),
                "laps": list(check.laps),
                "ok": result == "pass",
                "found": [],
            }
            for check in scenario.checks
        ]
        return (
            {
                "title": scenario.title,
                "status": scenario.status,
                "kind": scenario.kind,
                "result": result,
                "reason": "",
                "checks": checks,
            },
            [],
        )

    monkeypatch.setattr(bench_module, "run_scenario", fake_run_scenario)


def test_keep_missing_preserves_only_saved_non_skipped_rows() -> None:
    rows = {
        "guard": {"result": "skipped", "reason": "source not found", "current": True},
        "alpha-guard": {"result": "skipped", "reason": "source not found"},
        "hash-mismatch": {"result": "skipped", "reason": "sha256 mismatch"},
        "missing-baseline": {"result": "skipped", "reason": "source not found"},
        "skipped-baseline": {"result": "skipped", "reason": "source not found"},
    }
    old_guard = {"result": "pass", "reason": "", "saved": True}
    baseline = _card(
        {
            "guard": old_guard,
            "alpha-guard": _scenario("pass"),
            "hash-mismatch": _scenario("pass"),
            "skipped-baseline": _scenario("skipped"),
        }
    )

    kept_rows, kept = keep_missing(rows, baseline)

    assert kept == ["alpha-guard", "guard"]
    assert kept_rows["guard"] == {
        **old_guard,
        "reason": "kept from baseline: source not found",
    }
    assert kept_rows["hash-mismatch"] == rows["hash-mismatch"]
    assert kept_rows["missing-baseline"] == rows["missing-baseline"]
    assert kept_rows["skipped-baseline"] == rows["skipped-baseline"]
    unchanged_rows, unchanged_kept = keep_missing(rows, None)
    assert unchanged_rows == rows
    assert unchanged_kept == []


@pytest.mark.parametrize("damage", [{"lap": 1}, {"lap": 1, "wing_pct": 60, "positions_lost": 4}])
def test_damage_scenario_key(tmp_path: Path, damage: dict[str, int]) -> None:
    path = _write_scenario(tmp_path, _scenario_data(mutations={"damage": damage}))
    assert parse_scenario(path).mutations["damage"] == damage


@pytest.mark.parametrize(
    "damage", [{}, {"lap": True}, {"lap": 0}, {"lap": 1, "wing_pct": 101}, {"lap": 1, "extra": 2}]
)
def test_invalid_damage_scenario_key(tmp_path: Path, damage: dict[str, int]) -> None:
    path = _write_scenario(tmp_path, _scenario_data(mutations={"damage": damage}))
    with pytest.raises(ValueError):
        parse_scenario(path)


def test_parse_scenario_and_sort_by_id(tmp_path: Path) -> None:
    _write_scenario(tmp_path, _scenario_data(id="zeta"))
    _write_scenario(tmp_path, _scenario_data(id="alpha", mutations={}))

    scenarios = load_scenarios(tmp_path)

    assert [scenario.id for scenario in scenarios] == ["alpha", "zeta"]
    assert scenarios[0].kind == "real"
    assert scenarios[1].kind == "real"
    assert scenarios[0].checks == (ScenarioCheck("fire", ("sc_deployed",), (3, 4)),)


def test_load_scenarios_skips_radio_grades_workbook(tmp_path: Path) -> None:
    _write_scenario(tmp_path, _scenario_data())
    (tmp_path / "radio-priority-grades.yaml").write_text("kind: radio-grades\nscenarios: []\n")

    scenarios = load_scenarios(tmp_path)

    assert [scenario.id for scenario in scenarios] == ["case"]


@pytest.mark.parametrize(
    ("update", "key"),
    [
        ({"extra": True}, "extra"),
        ({"source": {"session_uid": "0x1234", "sha256": "a" * 64, "extra": True}}, "source.extra"),
        ({"mutations": {"extra": True}}, "mutations.extra"),
        ({"expect": {"fire": [], "extra": []}}, "expect.extra"),
        (
            {"expect": {"fire": [{"rule": "sc_deployed", "laps": [1, 2], "extra": True}]}},
            "expect.fire[0].extra",
        ),
    ],
)
def test_parse_scenario_rejects_unknown_keys(
    tmp_path: Path, update: dict[str, Any], key: str
) -> None:
    data = _scenario_data(**update)
    path = _write_scenario(tmp_path, data)

    with pytest.raises(ValueError, match=key.replace("[", r"\[").replace("]", r"\]")) as error:
        parse_scenario(path)

    assert str(path) in str(error.value)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"status": "maybe"}, "status"),
        ({"id": "other"}, "file stem"),
        (
            {"expect": {"fire": [{"rule": "sc_deployed", "laps": [0, 2]}]}},
            "1 <= start <= end",
        ),
        (
            {"expect": {"fire": [{"rule": [], "laps": [1, 2]}]}},
            "non-empty list",
        ),
    ],
)
def test_parse_scenario_rejects_invalid_values(
    tmp_path: Path, update: dict[str, Any], message: str
) -> None:
    data = _scenario_data(**update)
    if "id" in update:
        path = tmp_path / "case.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    else:
        path = _write_scenario(tmp_path, data)

    with pytest.raises(ValueError, match=message):
        parse_scenario(path)


def test_evaluate_checks_uses_any_rule_inclusive_laps_and_whole_session_found() -> None:
    checks = [
        ScenarioCheck("fire", ("sc_deployed", "vsc_deployed"), (3, 4)),
        ScenarioCheck("absent", ("box_now",), (1, 2)),
        ScenarioCheck("absent", ("sc_deployed",), (2, 2)),
    ]

    result = evaluate_checks(
        checks,
        [("sc_deployed", 2), ("vsc_deployed", 4), ("box_now", 5)],
    )

    assert [check["ok"] for check in result] == [True, True, False]
    assert result[0]["found"] == [2, 4]
    assert result[1]["found"] == [5]


def test_scorecard_counts_non_skipped_scenarios_and_real_outcomes() -> None:
    card = build_scorecard(
        {
            "failed": _scenario("error", status="guard", checks=[_check(ok=False)]),
            "skipped": _scenario("skipped", status="target", checks=[_check(ok=False)]),
        },
        [
            {
                "label": "good",
                "call_id": "call-1",
                "metric": "stop_cost_s",
                "rule_id": "pit",
                "error": -4.2,
            },
            {
                "label": "wrong",
                "call_id": "call-2",
                "metric": "stop_cost_s",
                "rule_id": "pit",
                "error": 3.2,
            },
            {
                "label": "good",
                "call_id": "plan:event",
                "metric": "laps_of_pace",
                "rule_id": "plan",
                "error": 2.0,
            },
        ],
        created_at="fixed",
    )

    assert card["created_at"] == "fixed"
    assert card["metrics"]["checks_total"] == 1
    assert card["metrics"]["checks_passed"] == 0
    assert card["metrics"]["guards_total"] == 1
    assert card["metrics"]["targets_total"] == 0
    assert card["metrics"]["real_graded"] == 2
    assert card["metrics"]["real_good"] == 1
    assert card["metrics"]["real_accuracy"] == 0.5
    assert card["metrics"]["by_rule"] == {"pit": {"good": 1, "wrong": 1}}
    assert card["metrics"]["stop_cost_mae_s"] == 3.7
    assert card["metrics"]["laps_of_pace_mae"] == 2.0


def test_compare_failure_rule_one_guard_failure() -> None:
    current = _card({"guard": _scenario("error", status="guard")})

    assert compare(current, _card()).failures == ["guard scenario guard error"]


def test_compare_failure_rule_two_scenario_regression() -> None:
    baseline = _card({"case": _scenario("pass")})
    current = _card({"case": _scenario("fail")})

    assert any("scenario case regressed" in item for item in compare(current, baseline).failures)


def test_compare_failure_rule_three_check_regression() -> None:
    check = _check()
    baseline = _card({"case": _scenario("pass", checks=[check])})
    current = _card({"case": _scenario("pass", checks=[{**check, "ok": False}])})

    assert any("check regressed in case" in item for item in compare(current, baseline).failures)


def test_compare_failure_rule_four_accuracy_threshold() -> None:
    baseline = _card(metrics={"real_graded": 5, "real_accuracy": 0.8})
    current = _card(metrics={"real_graded": 5, "real_accuracy": 0.77})

    assert any("real accuracy dropped" in item for item in compare(current, baseline).failures)


def test_compare_failure_rule_five_per_rule_accuracy_threshold() -> None:
    baseline = _card(metrics={"by_rule": {"pit": {"good": 4, "wrong": 1}}})
    current = _card(metrics={"by_rule": {"pit": {"good": 3, "wrong": 2}}})

    assert any(
        "rule accuracy dropped for pit" in item for item in compare(current, baseline).failures
    )


@pytest.mark.parametrize("metric", ["stop_cost_mae_s", "laps_of_pace_mae"])
def test_compare_failure_rule_six_mae_increase(metric: str) -> None:
    baseline = _card(metrics={metric: 10.0})
    current = _card(metrics={metric: 11.1})

    assert any(metric in item for item in compare(current, baseline).failures)


def test_compare_metric_rules_do_not_trigger_with_fewer_than_five() -> None:
    baseline = _card(
        metrics={
            "real_graded": 4,
            "real_accuracy": 1.0,
            "by_rule": {"pit": {"good": 4, "wrong": 0}},
        }
    )
    current = _card(
        metrics={
            "real_graded": 4,
            "real_accuracy": 0.0,
            "by_rule": {"pit": {"good": 0, "wrong": 4}},
        }
    )

    assert compare(current, baseline).failures == []


def test_compare_incomplete_for_skipped_baseline_scenario_and_guard() -> None:
    baseline = _card({"old": _scenario("pass")})
    current = _card(
        {
            "old": _scenario("skipped"),
            "guard": _scenario("skipped", status="guard"),
        }
    )

    result = compare(current, baseline)

    assert result.status == "incomplete"
    assert result.incomplete == ["guard scenario guard skipped", "scenario old skipped"]


def test_compare_no_baseline() -> None:
    result = compare(_card(), None)

    assert result.status == "pass"
    assert result.failures == []
    assert result.incomplete == []
    assert result.improvements == ["no baseline"]


def test_first_baseline_failing_guard_fails_and_update_is_refused(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    guard = _card({"guard": _scenario("fail", status="guard")})
    result = compare(guard, None)
    assert result.status == "fail"
    assert result.failures == ["guard scenario guard fail"]

    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _write_scenario(scenarios, _scenario_data(id="guard", status="guard"))
    _patch_bench_run(monkeypatch, {"guard": "fail"})

    exit_code = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--update-baseline",
            "--note",
            "must not update",
        ]
    )

    assert exit_code == 1
    assert "baseline not updated, gate is fail" in capsys.readouterr().out
    assert not (scenarios / "baseline.json").exists()


def test_compare_target_error_is_a_failure() -> None:
    result = compare(_card({"target": _scenario("error")}), _card())

    assert result.status == "fail"
    assert result.failures == ["scenario target error"]


def test_compare_removed_scenario_is_changed() -> None:
    result = compare(_card(), _card({"removed": _scenario("pass")}))

    assert result.status == "incomplete"
    assert result.changed == ["scenario removed removed"]


def test_bench_only_against_full_baseline_is_incomplete(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _write_scenario(scenarios, _scenario_data(id="case-a"))
    _write_scenario(scenarios, _scenario_data(id="case-b"))
    (scenarios / "baseline.json").write_text(
        json.dumps(
            _card(
                {
                    "case-a": _scenario("pass"),
                    "case-b": _scenario("pass"),
                }
            )
        )
    )
    _patch_bench_run(monkeypatch)

    exit_code = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--only",
            "case-a",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["gate"]["status"] == "incomplete"
    assert output["gate"]["incomplete"] == ["scenario case-b not run"]
    assert output["gate"]["changed"] == []


def test_bench_accepts_changed_passing_check_for_baseline_update(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _write_scenario(
        scenarios,
        _scenario_data(
            id="case",
            expect={"fire": [{"rule": "sc_deployed", "laps": [4, 5]}]},
        ),
    )
    (scenarios / "baseline.json").write_text(
        json.dumps(
            _card(
                {
                    "case": _scenario(
                        "pass",
                        checks=[_check(laps=(3, 4))],
                    )
                }
            )
        )
    )
    _patch_bench_run(monkeypatch)

    exit_code = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--update-baseline",
            "--accept-changes",
            "--note",
            "accept changed check",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    changed = ["passing check removed or changed in case: fire sc_deployed laps 3-4"]
    assert exit_code == 2
    assert output["gate"]["status"] == "incomplete"
    assert output["gate"]["changed"] == changed
    assert output["gate"]["accepted_changes"] == changed
    baseline = json.loads((scenarios / "baseline.json").read_text())
    assert baseline["scenarios"]["case"]["checks"][0]["laps"] == [4, 5]


def test_bench_update_baseline_refuses_changed_without_accept(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _write_scenario(
        scenarios,
        _scenario_data(
            id="case",
            expect={"fire": [{"rule": "sc_deployed", "laps": [4, 5]}]},
        ),
    )
    (scenarios / "baseline.json").write_text(
        json.dumps(
            _card(
                {
                    "case": _scenario(
                        "pass",
                        checks=[_check(laps=(3, 4))],
                    )
                }
            )
        )
    )
    _patch_bench_run(monkeypatch)

    exit_code = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--update-baseline",
            "--note",
            "reject changed check",
            "--json",
        ]
    )

    captured = capsys.readouterr().out
    json_output, refusal = captured.split("\nbench:", maxsplit=1)
    output = json.loads(json_output)
    changed = ["passing check removed or changed in case: fire sc_deployed laps 3-4"]
    assert exit_code == 2
    assert output["gate"]["changed"] == changed
    assert output["gate"]["accepted_changes"] == []
    assert "bench:" + refusal == (
        "bench: baseline not updated, changed items need --accept-changes "
        "after the expectation change is approved\n"
    )
    baseline = json.loads((scenarios / "baseline.json").read_text())
    assert baseline["scenarios"]["case"]["checks"][0]["laps"] == [3, 4]
    assert not (scenarios / "history.jsonl").exists()


def test_bench_accept_changes_requires_update_baseline(tmp_path: Path, capsys) -> None:
    scenarios = tmp_path / "scenarios"
    exit_code = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--accept-changes",
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().out.strip() == ("bench: --accept-changes requires --update-baseline")


def test_bench_keep_missing_requires_update_baseline(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "bench",
            "--scenarios",
            str(tmp_path / "scenarios"),
            "--keep-missing",
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().out.strip() == "bench: --keep-missing requires --update-baseline"


def test_bench_keep_missing_preserves_baseline_during_update(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import pitwall.learning.bench as bench_module

    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _write_scenario(scenarios, _scenario_data(id="guard", status="guard"))
    _write_scenario(scenarios, _scenario_data(id="new", status="target"))
    old_guard = _scenario("pass", status="guard", checks=[_check()])
    baseline = _card({"guard": old_guard})
    baseline_path = scenarios / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    def fake_find_source(scenario, index, digests=None):
        return (None, "source not found") if scenario.id == "guard" else (Path("new.f1bin"), None)

    def fake_run_scenario(scenario, source, settings, *, rules_dir, skip_reason):
        result = "skipped" if skip_reason else "pass"
        checks = [
            {
                "type": check.type,
                "rules": list(check.rules),
                "laps": list(check.laps),
                "ok": result == "pass",
                "found": [],
            }
            for check in scenario.checks
        ]
        return (
            {
                "title": scenario.title,
                "status": scenario.status,
                "kind": scenario.kind,
                "result": result,
                "reason": skip_reason or "",
                "checks": checks,
            },
            [],
        )

    monkeypatch.setattr(bench_module, "find_source", fake_find_source)
    monkeypatch.setattr(bench_module, "run_scenario", fake_run_scenario)
    args = [
        "bench",
        "--scenarios",
        str(scenarios),
        "--recordings",
        str(recordings),
        "--update-baseline",
        "--note",
        "add scenario with missing source",
        "--json",
    ]

    exit_code = main(args)

    captured = capsys.readouterr().out
    output = json.loads(captured.split("\nbench:", maxsplit=1)[0])
    assert exit_code == 1
    assert output["gate"]["status"] == "fail"
    assert output["gate"]["failures"] == [
        "check regressed in guard: fire ['sc_deployed'] laps [3, 4]"
    ]
    assert output["gate"]["incomplete"] == [
        "guard scenario guard skipped",
        "scenario guard skipped",
    ]
    assert json.loads(baseline_path.read_text()) == baseline
    assert not (scenarios / "history.jsonl").exists()

    exit_code = main([*args[:-1], "--keep-missing", "--json"])

    output = json.loads(capsys.readouterr().out)
    expected_guard = {**old_guard, "reason": "kept from baseline: source not found"}
    assert exit_code == 0
    assert output["gate"]["status"] == "pass"
    assert output["gate"]["kept"] == ["guard"]
    assert output["scorecard"]["scenarios"]["guard"] == expected_guard
    assert output["scorecard"]["metrics"]["guards_passed"] == 1
    written_baseline = json.loads(baseline_path.read_text())
    assert written_baseline["scenarios"]["guard"] == expected_guard
    assert "new" in written_baseline["scenarios"]
    history = (scenarios / "history.jsonl").read_text().splitlines()
    assert json.loads(history[-1])["kept"] == ["guard"]

    exit_code = main([*args[:-1], "--keep-missing"])

    assert exit_code == 0
    assert "kept: guard" in capsys.readouterr().out


def test_scorecard_accuracy_deduplicates_session_call_pairs() -> None:
    card = build_scorecard(
        {},
        [
            {"session_uid": 7, "call_id": "call-1", "label": "good", "rule_id": "pit"},
            {"session_uid": 7, "call_id": "call-1", "label": "wrong", "rule_id": "pit"},
            {"session_uid": 8, "call_id": "call-1", "label": "wrong", "rule_id": "pit"},
        ],
    )

    assert card["metrics"]["real_graded"] == 2
    assert card["metrics"]["real_good"] == 1
    assert card["metrics"]["real_accuracy"] == 0.5
    assert card["metrics"]["by_rule"] == {"pit": {"good": 1, "wrong": 1}}


def test_compare_lists_improvements() -> None:
    baseline = _card(
        {"case": _scenario("fail")},
        {
            "score": 62.5,
            "real_accuracy": 0.5,
            "stop_cost_mae_s": 4.2,
        },
    )
    current = _card(
        {"case": _scenario("pass")},
        {
            "score": 75.0,
            "real_accuracy": 0.75,
            "stop_cost_mae_s": 3.2,
        },
    )

    improvements = compare(current, baseline).improvements

    assert "score 62.5 -> 75.0 (+12.5)" in improvements
    assert "scenario case fail -> pass" in improvements
    assert "real_accuracy 0.5000 -> 0.7500 (+0.2500)" in improvements
    assert "stop_cost_mae_s 4.200 -> 3.200 (-1.000)" in improvements


@pytest.mark.parametrize(
    ("entries", "window", "expected"),
    [
        ([], 5, "not enough history"),
        ([{"score": 100, "targets_passed": 1}], 5, "not enough history"),
        (
            [
                {"score": 100, "targets_passed": 1},
                {"score": 99, "targets_passed": 1},
                {"score": 90, "targets_passed": 1},
            ],
            2,
            "degrading",
        ),
        ([{"score": 50, "targets_passed": 2}] * 5, 5, "stagnant"),
        (
            [{"score": 50, "targets_passed": 1}, {"score": 60, "targets_passed": 2}],
            5,
            "improving",
        ),
        (
            [{"score": 50, "targets_passed": 1}, {"score": 50, "targets_passed": 1}],
            5,
            "flat",
        ),
    ],
)
def test_trend_verdicts(entries: list[dict[str, Any]], window: int, expected: str) -> None:
    assert trend(entries, window) == expected


def test_trend_ignores_outlier_before_recent_window() -> None:
    entries = [{"score": 100, "targets_passed": 1}] + [
        {"score": 50, "targets_passed": 1} for _ in range(5)
    ]

    assert trend(entries, window=5) == "stagnant"


def test_ops_from_options_errors() -> None:
    with pytest.raises(ValueError, match="derive requires at least one mutation"):
        ops_from_options(None, None, False, None)
    with pytest.raises(ValueError, match="--vsc requires --inject-sc"):
        ops_from_options(None, None, True, None)
    with pytest.raises(ValueError, match="positive, ascending"):
        ops_from_options(None, "4-3", False, None)


def test_teammate_pit_scenario_mutation_validation(tmp_path: Path) -> None:
    ops = _scenario_ops({"teammate_pit": 11}, tmp_path / "scenario.yaml")
    assert [op.label for op in ops] == ["teammate_pit=11"]
    for value in (True, "11", 11.0):
        with pytest.raises(ValueError, match="mutations.teammate_pit must be an integer"):
            _scenario_ops({"teammate_pit": value}, tmp_path / "scenario.yaml")


def _bench_cli_argv(tmp_path: Path) -> list[str]:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    uid = 0x1234_5678_9ABC_DEF0
    source = write_packet_stream(
        recordings / "source.f1bin",
        race_stream(RaceSpec(laps=6, dt=1.0, session_uid=uid)),
        session_uid=uid,
        metadata={"synthetic": False},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    _write_scenario(
        scenarios,
        {
            **_scenario_data(id="scenario-a", status="target"),
            "source": {"session_uid": hex(uid), "sha256": digest},
            "mutations": {"inject_sc": "3-3"},
            "expect": {"fire": [{"rule": "sc_deployed", "laps": [3, 3]}]},
        },
    )
    _write_scenario(
        scenarios,
        {
            **_scenario_data(id="scenario-b", status="guard"),
            "source": {"session_uid": hex(uid), "sha256": digest},
            "mutations": {"inject_sc": "3-3"},
            "expect": {"absent": [{"rule": "sc_deployed", "laps": [1, 6]}]},
        },
    )
    _write_scenario(
        scenarios,
        {
            **_scenario_data(id="scenario-c", status="target"),
            "source": {"session_uid": hex(uid), "sha256": "0" * 64},
            "mutations": {},
        },
    )
    (scenarios / "baseline.json").write_text(
        json.dumps({"version": 1, "scenarios": {}, "metrics": {}})
    )
    return [
        "bench",
        "--scenarios",
        str(scenarios),
        "--recordings",
        str(recordings),
        "--rules",
        str(rules),
    ]


def test_bench_cli_replays_scenarios_with_isolated_scorecard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main([*_bench_cli_argv(tmp_path), "--json"])

    output = json.loads(capsys.readouterr().out)
    scorecard = output["scorecard"]
    assert result == 1
    assert scorecard["scenarios"]["scenario-a"]["result"] == "pass"
    assert scorecard["scenarios"]["scenario-b"]["result"] == "fail"
    assert scorecard["scenarios"]["scenario-c"]["result"] == "skipped"
    assert scorecard["scenarios"]["scenario-c"]["reason"] == "sha256 mismatch"
    assert output["gate"]["status"] == "fail"


def test_bench_parallel_matches_sequential(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pitwall.learning import bench as bench_module

    argv = _bench_cli_argv(tmp_path)
    with patch.object(bench_module, "datetime") as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        with patch.object(
            bench_module, "_file_sha256", wraps=bench_module._file_sha256
        ) as hash_file:
            sequential_exit = main([*argv, "--jobs", "1", "--json"])
            sequential = capsys.readouterr()
            assert hash_file.call_count == 1
        parallel_exit = main([*argv, "--jobs", "2", "--json"])
        parallel = capsys.readouterr()

    sequential_output = json.loads(sequential.out)
    parallel_output = json.loads(parallel.out)
    assert sequential_exit == parallel_exit
    assert sequential_output["scorecard"] == parallel_output["scorecard"]
    assert sequential_output["gate"] == parallel_output["gate"]
    assert len(re.findall(r"^bench \d/3: .+$", sequential.err, re.MULTILINE)) == 3
    assert len(re.findall(r"^bench \d/3: .+$", parallel.err, re.MULTILINE)) == 3


def test_bench_jobs_one_runs_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import concurrent.futures

    _patch_bench_run(monkeypatch)

    def fail_executor(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ProcessPoolExecutor should not be constructed")

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", fail_executor)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    uid = 0x1234_5678_9ABC_DEF0
    source = write_packet_stream(
        recordings / "source.f1bin",
        race_stream(RaceSpec(laps=2, dt=1.0, session_uid=uid)),
        session_uid=uid,
        metadata={"synthetic": False},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for scenario_id in ("scenario-a", "scenario-b"):
        _write_scenario(
            scenarios,
            {
                **_scenario_data(id=scenario_id),
                "source": {"session_uid": hex(uid), "sha256": digest},
            },
        )

    result = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--rules",
            str(rules),
            "--jobs",
            "1",
        ]
    )

    assert result == 0
    assert capsys.readouterr().err.splitlines() == [
        "bench 1/2: scenario-a ...",
        "bench 2/2: scenario-b ...",
    ]


def test_bench_caps_workers_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import concurrent.futures
    import sys

    _patch_bench_run(monkeypatch)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    uid = 0x1234_5678_9ABC_DEF0
    source = write_packet_stream(
        recordings / "source.f1bin",
        race_stream(RaceSpec(laps=2, dt=1.0, session_uid=uid)),
        session_uid=uid,
        metadata={"synthetic": False},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for index in range(62):
        scenario_id = f"scenario-{index:02}"
        _write_scenario(
            scenarios,
            {
                **_scenario_data(id=scenario_id),
                "source": {"session_uid": hex(uid), "sha256": digest},
            },
        )
    worker_counts: list[int] = []

    class FakeExecutor:
        def __init__(self, max_workers: int, **kwargs: Any) -> None:
            worker_counts.append(max_workers)

        def __enter__(self) -> FakeExecutor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def submit(self, fn: Any, *args: Any, **kwargs: Any) -> concurrent.futures.Future[Any]:
            future: concurrent.futures.Future[Any] = concurrent.futures.Future()
            future.set_result(fn(*args, **kwargs))
            return future

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(sys, "platform", "win32")

    result = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--rules",
            str(rules),
            "--jobs",
            "64",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert worker_counts == [61]
    assert result == 0
    assert len(output["scorecard"]["scenarios"]) == 62


def test_find_source_hashes_each_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pitwall.learning.bench as bench_module

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    uid = 0x1234_5678_9ABC_DEF0
    source = write_packet_stream(
        recordings / "source.f1bin",
        race_stream(RaceSpec(laps=2, dt=1.0, session_uid=uid)),
        session_uid=uid,
        metadata={"synthetic": False},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    scenarios = [
        parse_scenario(
            _write_scenario(
                tmp_path,
                {
                    **_scenario_data(id=scenario_id),
                    "source": {"session_uid": hex(uid), "sha256": digest},
                },
            )
        )
        for scenario_id in ("scenario-a", "scenario-b")
    ]
    index = build_recording_index([recordings])
    original = bench_module._file_sha256
    calls = 0

    def count_hash(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(bench_module, "_file_sha256", count_hash)
    digests: dict[Path, str] = {}

    assert find_source(scenarios[0], index, digests) == (source, "")
    assert find_source(scenarios[1], index, digests) == (source, "")
    assert calls == 1
