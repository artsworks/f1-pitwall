from __future__ import annotations

import argparse

import pytest

from pitwall.cli import main
from pitwall.cli.parser import ADVANCED, EVERYDAY, build_parser


def _subparsers(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    return next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )


def test_layered_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    choices = set(_subparsers(parser).choices)
    assert set(EVERYDAY) | set(ADVANCED) == choices
    assert set(EVERYDAY).isdisjoint(ADVANCED)

    with pytest.raises(SystemExit) as exc:
        main(["-h"])
    assert exc.value.code == 0
    everyday = capsys.readouterr().out
    assert "Everyday commands:" in everyday
    assert any(line.startswith("  start") for line in everyday.splitlines())
    assert not any(line.startswith("  bench") for line in everyday.splitlines())

    with pytest.raises(SystemExit) as exc:
        main(["--help-all"])
    assert exc.value.code == 0
    full = capsys.readouterr().out
    assert "Advanced commands:" in full
    assert any(line.startswith("  bench") for line in full.splitlines())
    assert any(line.startswith("  trim") for line in full.splitlines())
    assert not any(line.startswith("  trim") for line in everyday.splitlines())


def test_derive_command_accepts_teammate_pit_option() -> None:
    args = build_parser().parse_args(
        ["derive", "latest", "--out", "derived.f1bin", "--teammate-pit", "11"]
    )
    assert args.teammate_pit == 11
