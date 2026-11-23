from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import pitwall.audio.gpu as gpu


def _run_returning(stdout: str, returncode: int = 0):  # type: ignore[no-untyped-def]
    def run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    return run


def test_gpu_status_parses_first_line(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gpu.subprocess, "run", _run_returning("97, 15200, 16303\n"))
    assert gpu.gpu_status() == "gpu 97 %, 15200/16303 MB"


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("nvidia-smi", 2.0)]
)
def test_gpu_status_returns_none_when_the_tool_is_unavailable(monkeypatch, error) -> None:  # type: ignore[no-untyped-def]
    def run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise error

    monkeypatch.setattr(gpu.subprocess, "run", run)
    assert gpu.gpu_status() is None


def test_gpu_status_returns_none_on_failure_exit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gpu.subprocess, "run", _run_returning("97, 15200, 16303\n", 1))
    assert gpu.gpu_status() is None


def test_gpu_status_returns_none_on_unparsable_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gpu.subprocess, "run", _run_returning("N/A\n"))
    assert gpu.gpu_status() is None
