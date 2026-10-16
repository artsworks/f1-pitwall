from __future__ import annotations

from dataclasses import replace
from typing import Any

from pitwall.state.session import SessionState, Snapshot

_SNAPSHOT = Snapshot(now=0.0)


def make_snapshot(now: float = 0.0, **fields: Any) -> Snapshot:
    return replace(_SNAPSHOT, now=now, **fields)


def make_state(**kw: Any) -> SessionState:
    return SessionState(**kw)
