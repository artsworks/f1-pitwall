"""Descriptive comparison of recorded sessions with and without radio calls."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import TypedDict

from pitwall.store.db import Database


class _SessionResult(TypedDict):
    uid: int
    clean: list[float]
    laps: int
    eligible_laps: int
    invalid_laps: int
    mistakes: int
    negative_call_grades: int
    calls: int


class _Pace(TypedDict):
    p25: float | None
    p50: float | None
    p75: float | None
    mean: float | None


class _ModeResult(TypedDict):
    session_uids: list[int]
    sessions: int
    completed_laps: int
    clean_green_laps: int
    lap_time_s: _Pace
    invalid_laps: int
    mistake_rate: float | None
    fired_calls: int
    negative_call_grades: int


class _TrackResult(TypedDict):
    track_id: int
    on: _ModeResult
    off: _ModeResult
    comparable: bool


class Evaluation(TypedDict):
    tracks: list[_TrackResult]
    unknown_mode_sessions: int
    note: str


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    low = int(rank)
    return round(
        ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (rank - low), 3
    )


def evaluate_corpus(db: Database, track_id: int | None = None) -> Evaluation:
    """Summarise observed outcomes; call mode is session-level, not a causal treatment."""
    groups: dict[int, dict[str, list[_SessionResult]]] = defaultdict(lambda: {"on": [], "off": []})
    unknown = 0
    for session in db.sessions(track_id):
        mode = session.get("calls_mode")
        if mode not in ("on", "off"):
            unknown += 1
            continue
        uid = int(session["uid"])
        laps = [lap for lap in db.laps_for(uid) if lap.lap_time_ms > 0]
        if not laps:
            continue
        clean = [lap.lap_time_ms / 1000 for lap in laps if lap.valid and lap.sc_status == 0]
        neutral_reasons = {"first_lap", "pitted", "after_in_lap", "safety_car", "red_flag"}
        eligible = [lap for lap in laps if not neutral_reasons.intersection(lap.invalid_reasons)]
        mistakes = sum(not lap.valid for lap in eligible)
        grade_count = sum(
            grade["grade"] in ("noise", "wrong", "too_late") for grade in db.grades_for_session(uid)
        )
        groups[int(session.get("track_id") or -1)][str(mode)].append(
            {
                "uid": uid,
                "clean": clean,
                "laps": len(laps),
                "eligible_laps": len(eligible),
                "invalid_laps": sum(not lap.valid for lap in laps),
                "mistakes": mistakes,
                "negative_call_grades": grade_count,
                "calls": sum(call["outcome"] == "fired" for call in db.calls_for_session(uid)),
            }
        )
    tracks: list[_TrackResult] = []
    for track, modes in sorted(groups.items()):
        by_mode: dict[str, _ModeResult] = {}
        for mode in ("on", "off"):
            sessions = modes[mode]
            times = [value for session in sessions for value in session["clean"]]
            lap_count = sum(session["laps"] for session in sessions)
            eligible_laps = sum(session["eligible_laps"] for session in sessions)
            mistakes = sum(session["mistakes"] for session in sessions)
            invalid_laps = sum(session["invalid_laps"] for session in sessions)
            calls = sum(session["calls"] for session in sessions)
            by_mode[mode] = {
                "session_uids": [session["uid"] for session in sessions],
                "sessions": len(sessions),
                "completed_laps": lap_count,
                "clean_green_laps": len(times),
                "lap_time_s": {
                    "p25": _percentile(times, 0.25),
                    "p50": _percentile(times, 0.5),
                    "p75": _percentile(times, 0.75),
                    "mean": round(statistics.fmean(times), 3) if times else None,
                },
                "invalid_laps": invalid_laps,
                "mistake_rate": round(mistakes / eligible_laps, 4) if eligible_laps else None,
                "fired_calls": calls,
                "negative_call_grades": sum(
                    session["negative_call_grades"] for session in sessions
                ),
            }
        tracks.append(
            {
                "track_id": track,
                "on": by_mode["on"],
                "off": by_mode["off"],
                "comparable": bool(modes["on"] and modes["off"]),
            }
        )
    return {
        "tracks": tracks,
        "unknown_mode_sessions": unknown,
        "note": (
            "Descriptive only: different sessions, weather, drivers and strategy can "
            "explain differences. Mistake rate counts invalid laps outside first, pit, "
            "safety-car and red-flag laps; all invalid laps are reported separately. "
            "Negative call grades are reported separately. Compare within track, not across tracks."
        ),
    }
