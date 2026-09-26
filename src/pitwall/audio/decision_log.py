"""JSONL decision log: one line per rule evaluation that mattered.

Optionally mirrors records into the SQLite store (calls/bookmarks tables)
via `db` + `session_uid_source`."""

from __future__ import annotations

import json
from collections.abc import Callable
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
        db: Any = None,
        session_uid_source: Callable[[], int | None] | None = None,
    ) -> None:
        self.config_hash = config_hash
        self.mindset = mindset
        self._fp = fp if fp is not None else (path.open("a") if path else None)
        self.db = db
        self._session_uid = session_uid_source

    def write(self, record: dict[str, Any]) -> None:
        record = {
            "config_hash": self.config_hash,
            "mindset": self.mindset,
            **record,
        }
        if self.db is not None and self._session_uid is not None:
            uid = self._session_uid()
            if uid is not None:
                self.db.insert_call(int(uid), record)
        if self._fp is None:
            return
        self._fp.write(json.dumps(record, default=str) + "\n")

    def flush(self) -> None:
        if self._fp is not None:
            self._fp.flush()

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
