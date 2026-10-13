"""pitwall CLI: start, replay, review, learning and voice tools."""

from __future__ import annotations

import logging

from pitwall.cli.common import RecordingNotFoundError
from pitwall.cli.parser import build_parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return args.func(args)  # type: ignore[no-any-return]
    except RecordingNotFoundError as exc:
        print(f"{args.command}: {exc}")
        return 1
