from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import pitwall.trainloop as trainloop
from pitwall.bench import GateResult, Scenario
from pitwall.config.loader import ConfigStore
from pitwall.rollout import RolloutResult, RolloutSpec


def _fake_bench(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    baseline = scenarios_dir / "baseline.json"
    baseline.write_text("{}\n", encoding="utf-8")
    scenario = Scenario(
        id="target",
        title="Target",
        status="target",
        source_uid=123,
        source_sha256="a" * 64,
        mutations={},
        checks=(),
    )
    monkeypatch.setattr(trainloop, "load_scenarios", lambda _path: [scenario])
    monkeypatch.setattr(trainloop, "build_recording_index", lambda _paths: {})
    monkeypatch.setattr(
        trainloop,
        "find_source",
        lambda _scenario, _index: (tmp_path / "source.f1bin.zst", ""),
    )
    monkeypatch.setattr(
        trainloop,
        "run_scenarios",
        lambda scenarios, _index, _settings, _rules, _jobs: (
            {
                current.id: {
                    "title": current.title,
                    "status": current.status,
                    "kind": "real",
                    "result": "pass",
                    "reason": "",
                    "checks": [],
                }
                for current in scenarios
            },
            [],
        ),
    )
    monkeypatch.setattr(
        trainloop,
        "compare",
        lambda *_args, **_kwargs: GateResult("fail", [], [], [], []),
    )
    monkeypatch.setattr(trainloop, "_rollout_spec", lambda _path: RolloutSpec(laps=6, cars=4))
    monkeypatch.setattr(
        trainloop,
        "run_rollouts",
        lambda *_args, **_kwargs: RolloutResult(3.0, 1.0, 500.0, 0.5, 0.2, 10, "cpu", 0.1),
    )
    monkeypatch.setattr(trainloop, "print", lambda *_args, **_kwargs: None, raising=False)


def test_train_loop_respects_max_iters_and_preserves_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_bench(monkeypatch, tmp_path)
    baseline = tmp_path / "scenarios" / "baseline.json"
    before = hashlib.sha256(baseline.read_bytes()).hexdigest()
    rules_before = len(ConfigStore(isolated=True).current().rules)
    args = trainloop.build_parser().parse_args(
        [
            "--target",
            "target",
            "--scenarios",
            str(tmp_path / "scenarios"),
            "--baseline",
            str(baseline),
            "--param",
            "free_stop_margin_s=0,1",
            "--param",
            "pit_min_laps_left=2,3",
            "--max-iters",
            "2",
            "--sims",
            "10",
        ]
    )

    code, report = trainloop.run_training_loop(args)

    assert code == 1
    assert report["stop_reason"] == "max_iters"
    assert len(report["tried"]) == 2
    assert len(report["untried"]) == 2
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == before
    assert len(ConfigStore(isolated=True).current().rules) == rules_before


def test_train_loop_respects_max_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_bench(monkeypatch, tmp_path)
    args = trainloop.build_parser().parse_args(
        [
            "--target",
            "target",
            "--scenarios",
            str(tmp_path / "scenarios"),
            "--param",
            "free_stop_margin_s=0,1",
            "--max-minutes",
            "0.005",
            "--sims",
            "10",
        ]
    )
    clock_values = iter((0.0, 0.2, 0.4, 0.5))

    code, report = trainloop.run_training_loop(args, clock=lambda: next(clock_values))

    assert code == 1
    assert report["stop_reason"] == "max_minutes"
    assert len(report["tried"]) == 1
    assert len(report["untried"]) == 1
