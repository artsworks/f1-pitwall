from __future__ import annotations

import importlib.util
import json

import pytest

from pitwall.cli import main
from pitwall.rollout import RolloutSpec, run_rollouts


def test_numpy_rollouts_are_deterministic_and_chunk_independent() -> None:
    spec = RolloutSpec(laps=12, cars=8, player_grid=5, sc_prob_per_lap=0.15)

    full = run_rollouts(spec, {}, sims=250, seed=23)
    chunked = run_rollouts(spec, {}, sims=250, seed=23, chunk=31)

    assert full.mean_finish_position == chunked.mean_finish_position
    assert full.std_finish_position == chunked.std_finish_position
    assert full.mean_race_time_s == chunked.mean_race_time_s
    assert full.gain_probability == chunked.gain_probability
    assert full.sc_stop_rate == chunked.sc_stop_rate
    assert full.device == "cpu"
    assert full.sims == 250
    assert 1 <= full.mean_finish_position <= spec.cars
    assert full.mean_race_time_s > 0
    assert 0 <= full.gain_probability <= 1
    assert 0 <= full.sc_stop_rate <= 1


def test_rollout_cli_returns_json(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(
        [
            "rollout",
            "--track",
            "7",
            "--laps",
            "6",
            "--cars",
            "4",
            "--sims",
            "12",
            "--seed",
            "5",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["track_id"] == 7
    assert output["sims"] == 12
    assert output["device"] == "cpu"
    assert output["mean_race_time_s"] > 0


def test_cuda_request_reports_missing_backend_or_device() -> None:
    if importlib.util.find_spec("torch") is not None:
        torch = pytest.importorskip("torch")
        if torch.cuda.is_available():
            pytest.skip("CUDA is available")

    with pytest.raises(RuntimeError, match="CUDA rollouts"):
        run_rollouts(RolloutSpec(laps=4), {}, sims=1, seed=1, device="cuda")


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="PyTorch is not installed")
def test_cuda_rollouts_are_close_to_numpy() -> None:
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    spec = RolloutSpec(laps=10, cars=6, player_grid=3, sc_prob_per_lap=0.1)

    cpu = run_rollouts(spec, {}, sims=2000, seed=7)
    cuda = run_rollouts(spec, {}, sims=2000, seed=7, device="cuda")

    standard_error = (cpu.std_finish_position**2 / cpu.sims) ** 0.5
    assert abs(cpu.mean_finish_position - cuda.mean_finish_position) <= max(4 * standard_error, 0.1)
