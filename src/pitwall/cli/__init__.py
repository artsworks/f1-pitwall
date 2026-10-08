"""pitwall CLI: start, replay, review, learning and voice tools."""

from __future__ import annotations

from pitwall.cli.common import RecordingNotFoundError
from pitwall.cli.parser import build_parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)  # type: ignore[no-any-return]
    except RecordingNotFoundError as exc:
        print(f"{args.command}: {exc}")
        return 1
