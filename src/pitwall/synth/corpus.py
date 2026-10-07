"""Offline synthetic race corpus generation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

from pitwall.synth.field import (
    FieldSpec,
    GeneratedRace,
    _canonical_spec,
    write_field_recording,
)


def _write_one(arguments: tuple[FieldSpec, str]) -> GeneratedRace:
    spec, out_dir = arguments
    return write_field_recording(spec, out_dir)


def generate_corpus(
    specs: Sequence[FieldSpec],
    out_dir: Path | str,
    jobs: int = 1,
) -> list[GeneratedRace]:
    """Write a corpus serially or with spawn-safe workers, then append its manifest."""
    folder = Path(out_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    arguments = [(spec, str(folder)) for spec in specs]
    if jobs <= 1:
        results = [_write_one(argument) for argument in arguments]
    else:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            results = list(executor.map(_write_one, arguments))
    results.sort(key=lambda race: (race.seed, race.spec_hash))
    specs_by_hash = {_canonical_spec(spec)[1]: spec for spec in specs}
    with (folder / "manifest.jsonl").open("a", encoding="utf-8") as manifest:
        for race in results:
            spec = specs_by_hash[race.spec_hash]
            manifest.write(
                json.dumps(
                    {
                        "path": str(race.path),
                        "session_uid": f"0x{race.session_uid:016x}",
                        "sha256": race.sha256,
                        "seed": race.seed,
                        "spec_hash": race.spec_hash,
                        "spec": asdict(spec),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    return results
