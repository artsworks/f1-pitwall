"""Budget metrics: tick, parse and process sampling."""

from __future__ import annotations

import pytest

from pitwall.cli import main
from pitwall.metrics import Metrics, _Window


def test_window_max() -> None:
    w = _Window()
    assert w.max() == 0.0
    w.add(3.0)
    w.add(7.5)
    w.add(1.0)
    assert w.max() == 7.5


def test_note_tick_records_ms() -> None:
    metrics = Metrics()
    metrics.note_tick(1.0, 1.0125)
    assert metrics.tick_ms.samples[0] == pytest.approx(12.5)


def test_note_parse_records_us() -> None:
    metrics = Metrics()
    metrics.note_parse(1.0, 1.00025)
    assert metrics.parse_us.samples[0] == pytest.approx(250.0)


def test_sample_process_cpu_share_of_machine() -> None:
    cpu_times = iter([0.0, 0.4])
    metrics = Metrics(
        process_time=lambda: next(cpu_times),
        rss_reader=lambda: 512.0,
        cpu_count=8,
    )
    metrics.sample_process(0.0)
    assert metrics.cpu_pct == 0.0
    metrics.sample_process(1.0)
    assert metrics.cpu_pct == pytest.approx(0.4 / 1.0 / 8 * 100.0)
    assert metrics.rss_mb == 512.0


def test_summary_budget_keys() -> None:
    metrics = Metrics()
    metrics.note_tick(0.0, 0.01)
    metrics.note_parse(0.0, 0.0001)
    summary = metrics.summary()
    assert set(summary["tick_ms"]) == {"p50", "p99", "max"}
    assert set(summary["parse_us"]) == {"p50", "p99", "mean"}
    assert set(summary["process"]) == {"cpu_pct", "rss_mb"}
    assert summary["tick_ms"]["p50"] == pytest.approx(10.0)
    assert summary["parse_us"]["p50"] == pytest.approx(100.0)


def test_bench_profile_rejects_update_baseline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bench", "--profile", "--update-baseline", "--note", "x"]) == 1
    assert (
        capsys.readouterr().out.strip() == "bench: --profile cannot be used with --update-baseline"
    )


def test_bench_profile_rejects_accept_changes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bench", "--profile", "--update-baseline", "--accept-changes", "--note", "x"]) == 1
    assert (
        capsys.readouterr().out.strip() == "bench: --profile cannot be used with --update-baseline"
    )
