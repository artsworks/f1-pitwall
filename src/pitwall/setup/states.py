"""Setup hashes and run segmentation for setup-state history."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pitwall.store.db import Database, LapRow

HASH_EXCLUDE = frozenset({"fuel_load"})


def setup_hash(fields: Mapping[str, Any]) -> str:
    """Hash sorted setup fields, rounding floats to three decimal places."""
    if not any(
        value not in (None, "", 0, 0.0) for key, value in fields.items() if key not in HASH_EXCLUDE
    ):
        return ""
    normalized = [
        (key, round(value, 3) if isinstance(value, float) else value)
        for key, value in sorted(fields.items())
        if key not in HASH_EXCLUDE
    ]
    if not normalized:
        return ""
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
    return "sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Run:
    session_uid: int
    compound: int
    setup_state_id: int | None
    start_lap: int
    end_lap: int
    laps: tuple[LapRow, ...]


def runs_for_session(db: Database, uid: int, car_idx: int = 0) -> list[Run]:
    """Split laps into tyre stints, then split each stint by setup state."""
    rows = sorted(db.laps_for(uid, car_idx), key=lambda row: row.lap_num)
    stints: list[list[LapRow]] = []
    for row in rows:
        if stints and (
            row.compound != stints[-1][-1].compound
            or row.tyre_age_laps <= stints[-1][-1].tyre_age_laps
        ):
            stints.append([])
        if not stints:
            stints.append([])
        stints[-1].append(row)

    runs: list[Run] = []
    for stint in stints:
        groups: list[list[LapRow]] = []
        for row in stint:
            if groups and row.setup_state_id != groups[-1][-1].setup_state_id:
                groups.append([])
            if not groups:
                groups.append([])
            groups[-1].append(row)
        for group in groups:
            runs.append(
                Run(
                    session_uid=uid,
                    compound=group[0].compound,
                    setup_state_id=group[0].setup_state_id,
                    start_lap=group[0].lap_num,
                    end_lap=group[-1].lap_num,
                    laps=tuple(group),
                )
            )
    return runs


def majority_state(rows: Sequence[LapRow]) -> int | None:
    """Return the most common non-null state, with ties resolved by later lap."""
    states = [row.setup_state_id for row in rows if row.setup_state_id is not None]
    if not states:
        return None
    counts = Counter(states)
    best_count = max(counts.values())
    tied = {state for state, count in counts.items() if count == best_count}
    return next(state for state in reversed(states) if state in tied)
