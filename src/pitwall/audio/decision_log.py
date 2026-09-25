"""JSONL decision log: one line per rule evaluation that mattered."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any


class DecisionLog:
    def __init__(
        self,
        path: Path | None = None,
        *,
        config_hash: str = "",
        mindset: str = "",
        fp: IO[str] | None = None,
    ) -> None:
        self.config_hash = config_hash
        self.mindset = mindset
        self._fp = fp if fp is not None else (path.open("a") if path else None)

    def write(self, record: dict[str, Any]) -> None:
        if self._fp is None:
            return
        record = {
            "config_hash": self.config_hash,
            "mindset": self.mindset,
            **record,
        }
        self._fp.write(json.dumps(record, default=str) + "\n")

    def flush(self) -> None:
        if self._fp is not None:
            self._fp.flush()

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
