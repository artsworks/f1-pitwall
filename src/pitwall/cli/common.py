from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pitwall.net.recording import list_recordings

if TYPE_CHECKING:
    from pitwall.config.models import Settings
    from pitwall.ingest import IngestResult
    from pitwall.store.db import Database

REC_HELP = "recording: path, file name, # from `pitwall recordings`, or latest (default)"
REC_LIST_HELP = "each a path, file name, # from `pitwall recordings`, or latest"


class RecordingNotFoundError(LookupError):
    pass


def _resolve_recording(arg: str | None, settings: Settings) -> Path:
    """Resolve None/"latest", an index into `pitwall recordings`, a bare file name in
    the recordings folder, or a path."""
    directory = Path(settings.recording.directory)
    if arg is None or arg == "latest":
        found = list_recordings(directory)
        if not found:
            raise RecordingNotFoundError(f"no recordings in {directory}")
        path = found[0]
    elif Path(arg).expanduser().exists():
        path = Path(arg).expanduser()
    elif arg.isdigit():
        found = list_recordings(directory)
        if int(arg) >= len(found):
            raise RecordingNotFoundError(
                f"no recording #{arg} in {directory} ({len(found)} found, see pitwall recordings)"
            )
        path = found[int(arg)]
    else:
        candidates = [directory / arg, directory / f"{arg}.f1bin", directory / f"{arg}.f1bin.zst"]
        path = next((c for c in candidates if c.exists()), Path())
        if path == Path():
            raise RecordingNotFoundError(f"recording not found: {arg}")
    print(f"recording: {path}", file=sys.stderr)
    return path


def _resolve_recordings(args: list[str], settings: Settings) -> list[str]:
    """Resolve each entry; glob patterns pass through for expand_paths."""
    import glob

    return [a if glob.has_magic(a) else str(_resolve_recording(a, settings)) for a in args]


def db_from_args(args: argparse.Namespace, settings: Settings) -> Database | None:
    from pitwall.store.db import Database, open_configured

    return Database(args.db) if args.db else open_configured(settings)


def open_db(args: argparse.Namespace, settings: Settings, name: str) -> Database | None:
    db = db_from_args(args, settings)
    if db is None:
        print(f"{name}: persistence disabled")
    return db


def print_ingest_status(result: IngestResult) -> None:
    print(
        f"{result.session_uid} {result.status} {result.path}"
        + (f": {result.error}" if result.error else "")
    )


def ingest_paths(
    args: argparse.Namespace,
    db: Database,
    settings: Settings,
    out_dir: Path | None = None,
    *,
    show: Callable[[IngestResult], None] = print_ingest_status,
) -> int:
    from pitwall.ingest import ingest_recordings

    results = ingest_recordings(
        db,
        _resolve_recordings(args.paths, settings),
        settings,
        calls_mode=getattr(args, "calls_mode", None),
        out_dir=out_dir,
    )
    for result in results:
        show(result)
    return sum(result.status == "error" for result in results)


def emit_json_or(
    args: argparse.Namespace,
    result: Any,
    formatter: Callable[[Any], str | None],
) -> None:
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        out = formatter(result)
        if out is not None:
            print(out)


def add_db_arg(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--db", default=None, help="SQLite path (default: configured database)")


def add_json_arg(sub: argparse.ArgumentParser, help: str | None = None) -> None:
    sub.add_argument("--json", action="store_true", help=help)


def add_calls_mode_arg(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--calls-mode", choices=["on", "off"], default=None)


def add_recording_arg(
    sub: argparse.ArgumentParser,
    positional: str = "file",
    *,
    multiple: bool = False,
    required: bool = False,
    help_prefix: str = "",
) -> None:
    if multiple:
        sub.add_argument(
            positional,
            nargs="+" if required else "*",
            help=help_prefix + REC_LIST_HELP,
        )
    elif required:
        sub.add_argument(positional, help=REC_HELP)
    else:
        sub.add_argument(positional, nargs="?", default=None, help=REC_HELP)
