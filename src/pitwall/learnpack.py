from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from pitwall.store.db import Database, _uid_from_sql

PACK_VERSION = 1
LEDGER_NAME = "track_ledger.jsonl"
LATEST_NAME = "learning-latest.json"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _row_uid(row: object) -> int | None:
    if not isinstance(row, dict):
        return None
    uid = row.get("uid")
    return uid if isinstance(uid, int) and not isinstance(uid, bool) else None


def _sort_key(row: dict[str, Any]) -> tuple[bool, float, int]:
    started_at = _number(row.get("started_at"))
    return (started_at is None, started_at if started_at is not None else 0.0, int(row["uid"]))


def _ledger_path(path: Path) -> Path:
    path = path.expanduser()
    return path / LEDGER_NAME if path.is_dir() else path


def read_ledger(path: Path) -> dict[int, dict[str, Any]]:
    ledger_path = _ledger_path(Path(path))
    if not ledger_path.is_file():
        return {}
    rows: dict[int, dict[str, Any]] = {}
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        uid = _row_uid(row)
        if uid is None:
            continue
        row["uid"] = uid
        previous = rows.get(uid)
        old_ms = _number(previous.get("ms")) if previous is not None else None
        new_ms = _number(row.get("ms"))
        if previous is None or (new_ms is not None and (old_ms is None or new_ms > old_ms)):
            rows[uid] = row
    return rows


def merged_sessions(db: Database, pack_dir: Path) -> dict[int, dict[str, Any]]:
    rows = read_ledger(Path(pack_dir) / LEDGER_NAME)
    for row in db.session_track_ms():
        uid = _row_uid(row)
        if uid is None:
            continue
        previous = rows.get(uid)
        old_ms = _number(previous.get("ms")) if previous is not None else None
        new_ms = _number(row.get("ms"))
        if previous is None or (new_ms is not None and (old_ms is None or new_ms > old_ms)):
            rows[uid] = row
    return rows


def pack_track_minutes(db: Database, pack_dir: Path) -> dict[str, Any]:
    merged = merged_sessions(db, pack_dir)
    sessions = merged.values()
    total_ms = sum(_number(row.get("ms")) or 0.0 for row in sessions)
    laps = sum(int(_number(row.get("laps")) or 0) for row in sessions)
    return {
        "minutes": round(total_ms / 60_000, 1),
        "laps": laps,
        "sessions": len(merged),
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _ledger_text(sessions: dict[int, dict[str, Any]]) -> str:
    return "".join(
        json.dumps(row, default=str, separators=(",", ":")) + "\n"
        for row in sorted(sessions.values(), key=_sort_key)
    )


def _overlays(overlay_dir: Path | None) -> dict[str, dict[str, Any]]:
    root = (overlay_dir or (Path.home() / ".pitwall" / "tracks")).expanduser()
    result: dict[str, dict[str, Any]] = {}
    for path in root.glob("*.yaml"):
        try:
            overlay = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if isinstance(overlay, dict):
            result[path.stem] = overlay
    return result


def write_pack(
    db: Database,
    pack_dir: Path,
    keep_days: int = 30,
    now: datetime | float | None = None,
    overlay_dir: Path | None = None,
) -> Path:
    root = Path(pack_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    sessions = merged_sessions(db, root)
    _atomic_write(root / LEDGER_NAME, _ledger_text(sessions))

    if now is None:
        current = datetime.now()
    elif isinstance(now, (int, float)):
        current = datetime.fromtimestamp(now)
    else:
        current = now
    current = current.astimezone()
    grade_rows = db.all_grades()
    for row in grade_rows:
        if row.get("session_uid") is not None:
            row["session_uid"] = _uid_from_sql(int(row["session_uid"]))
    pack = {
        "pack_version": PACK_VERSION,
        "written_at": current.isoformat(),
        "db_user_version": int(db._conn.execute("PRAGMA user_version").fetchone()[0]),
        "track_minutes": pack_track_minutes(db, root),
        "sessions": sorted(sessions.values(), key=_sort_key),
        "model_params": [dataclasses.asdict(row) for row in db.all_params()],
        "model_params_quarantine": db.quarantined_params(),
        "call_grades": grade_rows,
        "track_overlays": _overlays(overlay_dir),
    }
    latest_path = root / LATEST_NAME
    content = json.dumps(pack, default=str, indent=2) + "\n"
    _atomic_write(latest_path, content)
    dated_path = root / f"learning-{current.date().isoformat()}.json"
    _atomic_write(dated_path, content)

    dated_files = sorted(root.glob("learning-????-??-??.json"), key=lambda item: item.name)
    for old in dated_files[: max(0, len(dated_files) - max(0, keep_days))]:
        old.unlink()
    return latest_path


def restore_pack(
    db: Database,
    path: Path,
    pack_dir: Path,
    overlay_dir: Path | None = None,
) -> dict[str, int]:
    pack = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(pack, dict):
        raise ValueError("learning pack must contain a JSON object")
    version = pack.get("pack_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("learning pack has an invalid pack_version")
    if version > PACK_VERSION:
        raise ValueError(
            f"learning pack version {version} is newer than supported version {PACK_VERSION}"
        )

    counts = {
        "model_params": 0,
        "model_params_quarantine": 0,
        "call_grades": 0,
        "track_overlays": 0,
        "sessions": 0,
    }
    for row in pack.get("model_params", []):
        if not isinstance(row, dict):
            continue
        current = db.get_param(int(row["track_id"]), int(row["compound"]), str(row["name"]))
        weight = float(row["weight"])
        if current is None or current.weight < weight:
            db.set_param(
                int(row["track_id"]),
                int(row["compound"]),
                str(row["name"]),
                float(row["value"]),
                weight,
            )
            counts["model_params"] += 1
    for row in pack.get("model_params_quarantine", []):
        if isinstance(row, dict) and db.insert_quarantine_if_absent(row):
            counts["model_params_quarantine"] += 1
    for row in pack.get("call_grades", []):
        if isinstance(row, dict) and db.insert_grade_if_absent(row):
            counts["call_grades"] += 1

    overlays = pack.get("track_overlays", {})
    if isinstance(overlays, dict):
        root = (overlay_dir or (Path.home() / ".pitwall" / "tracks")).expanduser()
        for track_id, overlay in overlays.items():
            if not isinstance(overlay, dict):
                continue
            try:
                filename_id = int(track_id)
            except (TypeError, ValueError):
                continue
            path = root / f"{filename_id}.yaml"
            if path.exists():
                continue
            _atomic_write(path, yaml.safe_dump(overlay, sort_keys=False))
            counts["track_overlays"] += 1

    root = Path(pack_dir).expanduser()
    sessions = merged_sessions(db, root)
    for row in pack.get("sessions", []):
        uid = _row_uid(row)
        if uid is None:
            continue
        previous = sessions.get(uid)
        old_ms = _number(previous.get("ms")) if previous is not None else None
        new_ms = _number(row.get("ms"))
        if previous is None or (new_ms is not None and (old_ms is None or new_ms > old_ms)):
            sessions[uid] = row
            counts["sessions"] += 1
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / LEDGER_NAME, _ledger_text(sessions))
    return counts
