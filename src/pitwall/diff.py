"""pitwall diff: compare two rules/config directories over the same
recording(s). Each recording is replayed at max speed under config A and
config B; fired decisions are compared by (rule_id, lap)."""

from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.store.db import Database

# Fired calls that land > 0.5 s apart under A vs B count as "different".
_TIME_MATCH_S = 0.5


def _fired_records(
    recording: Path, rules_dir: Path | None, mindset: str | None
) -> list[dict[str, Any]]:
    """Replay a recording under one config; return its fired decision rows."""
    overrides: dict[str, Any] = {}
    if mindset:
        overrides = {"mindset": {"active": mindset}}
    fp = io.StringIO()
    engine = build_engine(
        clock=VirtualClock(),
        overrides=overrides,
        rules_dir=rules_dir,
        decision_log_fp=fp,
        sinks=[],
        db=Database(":memory:"),
    )
    import asyncio

    asyncio.run(run_replay(recording, engine, None))
    engine.dispatcher.log.flush()
    fp.seek(0)
    return [json.loads(line) for line in fp if line.strip()]


def _by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("outcome") == "fired":
            out.setdefault((str(r.get("rule_id")), int(r.get("lap") or 0)), []).append(r)
    return out


def diff_one(
    recording: Path,
    a_rows: list[dict[str, Any]],
    b_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare fired records from two configs on one recording."""
    a, b = _by_key(a_rows), _by_key(b_rows)
    only_a: list[dict[str, Any]] = []
    only_b: list[dict[str, Any]] = []
    different: list[dict[str, Any]] = []
    for key, rows in a.items():
        if key not in b:
            only_a.extend(rows)
            continue
        for ar in rows:
            match = next(
                (
                    br
                    for br in b[key]
                    if abs(float(ar["t"]) - float(br["t"])) <= _TIME_MATCH_S
                    and ar.get("text") == br.get("text")
                ),
                None,
            )
            if match is None:
                different.append({"a": ar, "b": b[key][0]})
    for key, rows in b.items():
        if key not in a:
            only_b.extend(rows)
    by_lap: dict[int, dict[str, int]] = {}
    for row in a_rows + b_rows:
        if row.get("outcome") != "fired":
            continue
        lap = int(row.get("lap") or 0)
        by_lap.setdefault(lap, {"a": 0, "b": 0})
    for row in a_rows:
        if row.get("outcome") == "fired":
            by_lap[int(row.get("lap") or 0)]["a"] += 1
    for row in b_rows:
        if row.get("outcome") == "fired":
            by_lap[int(row.get("lap") or 0)]["b"] += 1
    per_priority: dict[str, dict[str, int]] = {}
    for side, rows in (("a", a_rows), ("b", b_rows)):
        for r in rows:
            if r.get("outcome") != "fired":
                continue
            p = str(r.get("priority"))
            per_priority.setdefault(p, {"a": 0, "b": 0})[side] += 1
    per_rule: dict[str, dict[str, int]] = {}
    for side, rows in (("a", a_rows), ("b", b_rows)):
        counts = Counter(str(r.get("rule_id")) for r in rows if r.get("outcome") == "fired")
        for rid, n in counts.items():
            per_rule.setdefault(rid, {"a": 0, "b": 0})[side] = n
    return {
        "recording": str(recording),
        "only_a": only_a,
        "only_b": only_b,
        "different": different,
        "summary": {
            "per_lap": {str(k): v for k, v in sorted(by_lap.items())},
            "per_priority": per_priority,
            "per_rule": per_rule,
            "totals": {
                "a": sum(1 for r in a_rows if r.get("outcome") == "fired"),
                "b": sum(1 for r in b_rows if r.get("outcome") == "fired"),
                "only_a": len(only_a),
                "only_b": len(only_b),
                "different": len(different),
            },
        },
    }


def run_diff(
    recordings: list[Path],
    a_dir: Path | None,
    b_dir: Path,
    *,
    a_mindset: str | None = None,
    b_mindset: str | None = None,
) -> dict[str, Any]:
    files = [
        diff_one(
            rec,
            _fired_records(rec, a_dir, a_mindset),
            _fired_records(rec, b_dir, b_mindset),
        )
        for rec in recordings
    ]
    totals: dict[str, int] = {"a": 0, "b": 0, "only_a": 0, "only_b": 0, "different": 0}
    for f in files:
        for k in totals:
            totals[k] += int(f["summary"]["totals"][k])
    return {"files": files, "totals": totals}


def format_diff(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for f in result["files"]:
        lines.append(f"== {f['recording']}")
        for label, rows in (("only-A", f["only_a"]), ("only-B", f["only_b"])):
            for r in rows:
                lines.append(
                    f"  {label}  {r.get('rule_id')} L{r.get('lap')} "
                    f"@ {float(r.get('t') or 0):.1f}s  {r.get('text')}"
                )
        for pair in f["different"]:
            a, b = pair["a"], pair["b"]
            lines.append(
                f"  diff    {a.get('rule_id')} L{a.get('lap')}"
                f'  A@{float(a.get("t") or 0):.1f}s "{a.get("text")}"'
                f'  B@{float(b.get("t") or 0):.1f}s "{b.get("text")}"'
            )
        t = f["summary"]["totals"]
        lines.append(
            f"  fired: A={t['a']} B={t['b']}  only-A={t['only_a']}"
            f" only-B={t['only_b']} different={t['different']}"
        )
        lines.append(f"  per-lap: {f['summary']['per_lap']}")
        lines.append(f"  per-priority: {f['summary']['per_priority']}")
        lines.append(f"  per-rule: {f['summary']['per_rule']}")
    if len(result["files"]) > 1:
        lines.append(f"corpus totals: {result['totals']}")
    return "\n".join(lines)
