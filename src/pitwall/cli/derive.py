from __future__ import annotations

import argparse
from pathlib import Path

from pitwall.cli.common import _resolve_recording
from pitwall.config.loader import ConfigStore
from pitwall.net.derive import derive_recording, ops_from_options


def cmd_derive(args: argparse.Namespace) -> int:
    source = _resolve_recording(args.file, ConfigStore().current())
    destination = Path(args.out).expanduser()
    ops = ops_from_options(
        args.wear_scale,
        args.inject_sc,
        args.vsc,
        args.penalty,
        args.teammate_pit,
    )
    summary = derive_recording(source, destination, ops)
    print(f"derived {summary.record_count} packets to {destination}")
    return 0
