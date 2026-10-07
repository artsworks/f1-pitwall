from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pitwall.bench import (
    ScenarioCheck,
    build_scorecard,
    compare,
    evaluate_checks,
    load_scenarios,
    parse_scenario,
    trend,
)
from pitwall.cli import main
from pitwall.derive import ops_from_options

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream


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
    import pitwall.bench as bench_module

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


def test_parse_scenario_and_sort_by_id(tmp_path: Path) -> None:
    _write_scenario(tmp_path, _scenario_data(id="zeta"))
    _write_scenario(tmp_path, _scenario_data(id="alpha", mutations={}))

    scenarios = load_scenarios(tmp_path)

    assert [scenario.id for scenario in scenarios] == ["alpha", "zeta"]
    assert scenarios[0].kind == "real"
    assert scenarios[1].kind == "real"
    assert scenarios[0].checks == (ScenarioCheck("fire", ("sc_deployed",), (3, 4)),)


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


def test_bench_cli_replays_scenarios_with_isolated_scorecard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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

    result = main(
        [
            "bench",
            "--scenarios",
            str(scenarios),
            "--recordings",
            str(recordings),
            "--rules",
            str(rules),
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    scorecard = output["scorecard"]
    assert result == 1
    assert scorecard["scenarios"]["scenario-a"]["result"] == "pass"
    assert scorecard["scenarios"]["scenario-b"]["result"] == "fail"
    assert scorecard["scenarios"]["scenario-c"]["result"] == "skipped"
    assert scorecard["scenarios"]["scenario-c"]["reason"] == "sha256 mismatch"
    assert output["gate"]["status"] == "fail"
