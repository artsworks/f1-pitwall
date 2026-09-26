"""FastAPI app: static dashboard, /api/health, /api/config, WS /ws."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pitwall.config.loader import ConfigStore
from pitwall.metrics import Metrics
from pitwall.server.hub import PROTOCOL_VERSION, Hub
from pitwall.state.session import Snapshot

WEB_DIR = Path(__file__).resolve().parents[3] / "web"
STALE_MS = 1000.0


def tyre_status(inner: float, cold_c: float, hot_c: float) -> str:
    if inner < cold_c:
        return "COLD"
    if inner > hot_c:
        return "HOT"
    return "OK"


def packet_age_ms(snapshot: Snapshot) -> float | None:
    """Age of the newest packet in `snapshot.now`'s clock domain (ms)."""
    if snapshot.last_packet_t is None:
        return None
    return max(0.0, (snapshot.now - snapshot.last_packet_t) * 1000.0)


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


PIT_BOARD_PHASES = ("pitting", "garage")


def pit_board_payload(
    snapshot: Snapshot, thresholds: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Full-screen pit board while pitting / in the garage: pressure target per
    corner, car setup as the game reports it, and what the next run has."""
    if snapshot.phase not in PIT_BOARD_PHASES:
        return None
    th = thresholds or {}
    advice = {c.corner: c for c in snapshot.pressure_advice} if snapshot.run_flying_s > 0 else {}
    tyres: dict[str, dict[str, Any]] = {}
    for name in ("fl", "fr", "rl", "rr"):
        now_psi = getattr(snapshot.setup_tyre_pressure, name) or None
        c = advice.get(name)
        target = c.target_psi if c and c.target_psi else None
        tyres[name] = {
            "psi": now_psi,
            "target_psi": target,
            "delta_psi": c.delta_psi if c else 0.0,
            "limited": c.limited if c else False,
            "edge": ("min" if c.wanted_psi < 0 else "max") if c and c.limited else None,
            "avg_c": c.avg_c if c else None,
            "applied": bool(
                c
                and not c.limited
                and target is not None
                and now_psi is not None
                and abs(now_psi - target) < 0.05
            ),
        }
    return {
        "has_advice": bool(advice),
        "advice_text": snapshot.pressure_advice_text if advice else "",
        "tyres": tyres,
        "setup": dict(snapshot.setup) or None,
        "fuel_laps": snapshot.fuel_remaining_laps,
        "fuel_need_laps": th.get("fuel_push_need_laps", 0.9),
        "ers_pct": snapshot.ers_store_pct,
        "ers_need_pct": snapshot.ers_need_pct or th.get("cool_ers_min_pct", 40.0),
    }


_COMPOUND_WORDS = {16: "SOFT", 17: "MEDIUM", 18: "HARD", 7: "INTER", 8: "WET"}


def _compound(visual: int) -> str | None:
    return _COMPOUND_WORDS.get(visual) or (f"C{visual}" if visual else None)


def strategy_payload(
    snapshot: Snapshot, thresholds: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Zone F / battle page contract (docs/15 §5, docs/18): pit window, the
    immediate ahead/behind rivals only (never a timing tower), undercut /
    overcut and a one-line stint plan. None outside races."""
    if snapshot.session_kind != "race" or not snapshot.race_phase:
        return None
    th = thresholds or {}
    drs_gap = float(th.get("drs_detection_gap_s", 1.0))
    own_ms = snapshot.predicted_lap_ms or int(snapshot.base_pace_ms)

    def rival(side: str) -> dict[str, Any] | None:
        ahead = side == "ahead"
        idx = snapshot.rival_ahead_idx if ahead else snapshot.rival_behind_idx
        if idx < 0:
            return None
        gap = snapshot.gap_ahead_s if ahead else snapshot.gap_behind_s
        pace = snapshot.rival_ahead_pace_ms if ahead else snapshot.rival_behind_pace_ms
        gap_f = _finite(gap)
        return {
            "idx": idx,
            "pos": snapshot.rival_ahead_pos if ahead else snapshot.rival_behind_pos,
            "name": snapshot.rival_ahead_name if ahead else snapshot.rival_behind_name,
            "gap_s": gap_f,
            "compound": _compound(
                snapshot.rival_ahead_compound if ahead else snapshot.rival_behind_compound
            ),
            "tyre_age": snapshot.rival_ahead_age if ahead else snapshot.rival_behind_age,
            # + = the rival is slower than us per lap
            "pace_delta_s": round((pace - own_ms) / 1000.0, 3) if pace and own_ms else None,
            "gap_trend_s": snapshot.gap_trend_ahead_s if ahead else snapshot.gap_trend_behind_s,
            "drs": gap_f is not None and gap_f < drs_gap and snapshot.safety_car_status == 0,
            "pitted": snapshot.rival_ahead_pitted if ahead else snapshot.rival_behind_pitted,
        }

    window = None
    if snapshot.pit_window_start > 0:
        window = {"start": snapshot.pit_window_start, "end": snapshot.pit_window_end}
    comp = _compound(snapshot.tyre_visual) or "--"
    lop = _finite(snapshot.laps_of_pace)
    if window is not None:
        stint = f"{comp} to L{window['start']}–{window['end']} · box · to flag"
    elif lop is not None and lop >= snapshot.laps_remaining > 0:
        stint = f"{comp} to flag · {snapshot.laps_remaining} laps"
    elif lop is not None:
        stint = f"{comp} · {lop:.0f} laps of pace left"
    else:
        stint = f"{comp} · deg model warming up"
    plan = None
    if snapshot.pit_plan:
        plan = {
            "kind": snapshot.pit_plan,
            "lap": snapshot.pit_plan_lap,
            "gain_s": snapshot.pit_plan_gain_s,
            "confidence": snapshot.pit_plan_confidence,
            "risk": snapshot.pit_plan_risk,
            "rival": snapshot.pit_plan_rival_name or None,
            "reason": snapshot.pit_plan_reason,
        }
    exit_rival = None
    if snapshot.rival_pit_exit_idx >= 0:
        exit_rival = {
            "name": snapshot.rival_pit_exit_name,
            "gap_s": _finite(snapshot.pit_exit_rival_gap_s),
        }
    return {
        "phase": snapshot.race_phase,
        "laps_remaining": snapshot.laps_remaining,
        "pit_window": window,
        "plan": plan,
        "ahead": rival("ahead"),
        "behind": rival("behind"),
        "drs": snapshot.drs_available,
        "undercut_s": snapshot.undercut_s or None,
        "overcut_s": snapshot.overcut_s or None,
        "stint_plan": stint,
        "pit_exit": {"clean": snapshot.pit_exit_clean, "rival": exit_rival},
        "laps_of_pace": lop,
        "pit_loss_s": snapshot.pit_loss_s or None,
        "pit_loss_source": snapshot.pit_loss_source or None,
        # Backend-owned fuel target (docs/15 open question 1): margin vs the
        # laps to the flag. Absent -> the client keeps laps remaining prominent.
        "fuel_delta_laps": (_finite(snapshot.fuel_margin_laps) if snapshot.fuel_source else None),
        "energy": {
            "per_lap_mj": snapshot.energy_per_lap_mj,
            "lap_delta_mj": snapshot.energy_lap_delta_mj,
            "laps_to_floor": _finite(snapshot.energy_laps_to_floor),
            "mode": snapshot.energy_mode or None,
        },
        "tyres": {
            "overheat": snapshot.overheat,
            "graining": snapshot.graining,
            "blister_max_pct": snapshot.blister_max_pct,
            "wear_per_lap_pct": snapshot.wear_per_lap_pct,
        },
        "restricted": snapshot.rival_data_restricted,
    }


def track_payload(snapshot: Snapshot) -> dict[str, Any]:
    """Track-awareness page: flags, SC/VSC, weather and forecast, penalties."""
    return {
        "phase": snapshot.race_phase or snapshot.phase,
        "safety_car": snapshot.safety_car_status,
        "sc_laps": snapshot.sc_laps,
        "weather": snapshot.weather_now,
        "rain_pct": [snapshot.rain_pct_now, snapshot.rain_pct_in_10, snapshot.rain_pct_in_30],
        "weather_crossover": snapshot.weather_crossover or None,
        "blue_flag": snapshot.blue_flag,
        "red_flag": snapshot.red_flag,
        "penalty_s": snapshot.penalty_s,
        "warnings": snapshot.warnings,
        "corner_cut_warnings": snapshot.corner_cut_warnings,
        "unserved": snapshot.unserved_drive_through + snapshot.unserved_stop_go,
        "cars_on_track": snapshot.cars_on_track,
        "pit_exit_clean": snapshot.pit_exit_clean,
        "gap_ahead_s": _finite(snapshot.gap_ahead_s),
        "gap_behind_s": _finite(snapshot.gap_behind_s),
    }


def setup_payload(snapshot: Snapshot) -> dict[str, Any] | None:
    """Read-only setup page (display only; no setup advice logic)."""
    if not snapshot.setup:
        return None
    return {
        "values": dict(snapshot.setup),
        "pressures": {
            k: getattr(snapshot.setup_tyre_pressure, k) or None for k in ("fl", "fr", "rl", "rr")
        },
    }


def quali_payload(
    snapshot: Snapshot, thresholds: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Zone F in qualifying (docs/15 §8): release window in the garage, lap vs
    cut-off while flying."""
    if snapshot.session_kind != "qualifying":
        return None
    out: dict[str, Any] = {
        "session_time_left": snapshot.session_time_left,
        "fresh_sets": snapshot.fresh_sets_current,
        "best_lap_ms": snapshot.player_best_lap_ms or None,
        "cutoff_ms": snapshot.quali_cutoff_ms or None,
        "through": snapshot.quali_through,
        "margin_ms": snapshot.quali_margin_ms if snapshot.quali_margin_kind else None,
        "margin_kind": snapshot.quali_margin_kind or None,
    }
    if snapshot.run_flying_s > 0 and snapshot.phase in ("in_lap", "pitting", "garage"):
        out["pressure"] = [
            {
                "corner": c.corner,
                "size": c.size,
                "delta_psi": c.delta_psi,
                "target_psi": c.target_psi or None,
                "avg_c": c.avg_c,
            }
            for c in snapshot.pressure_advice
        ]
    if snapshot.phase in ("garage", "pitting"):
        out["release"] = {
            "clean": snapshot.release_clean,
            "wait_s": _finite(snapshot.release_wait_s),
            "gap_ahead_s": _finite(snapshot.release_gap_ahead_s),
            "gap_behind_s": _finite(snapshot.release_gap_behind_s),
            "cars_on_track": snapshot.cars_on_track,
            "time_for_out_lap": snapshot.time_for_out_lap,
        }
    if snapshot.phase == "flying" and snapshot.projected_lap_ms:
        out["lap"] = {
            "projected_ms": snapshot.projected_lap_ms,
            "delta_ms": (
                snapshot.projected_lap_ms - snapshot.quali_cutoff_ms
                if snapshot.quali_cutoff_ms
                else None
            ),
            "abort": snapshot.abort_advised,
        }
    if snapshot.run_plan:
        out["plan"] = {"plan": snapshot.run_plan, "reason": snapshot.run_plan_reason}
    if snapshot.cool_lap and (not snapshot.cool_prep or snapshot.cool_extend):
        inner = snapshot.tyre_inner_ema_fast
        th = thresholds or {}
        out["cool"] = {
            "ers_min_pct": snapshot.ers_need_pct or th.get("cool_ers_min_pct", 40.0),
            "window_c": [
                th.get("pressure_window_low_c", 88.0),
                th.get("pressure_window_high_c", 102.0),
            ],
            "ers_pct": snapshot.ers_store_pct,
            "ers_mode": snapshot.ers_deploy_mode,
            "recharging": snapshot.ers_deploy_mode == th.get("ers_recharge_mode", -1),
            "plan_reason": snapshot.run_plan_reason or None,
            "extend": snapshot.cool_extend,
            "dist_to_hot_m": snapshot.dist_to_hot_mode_m if snapshot.track_length_m else None,
            "tyres": {"fl": inner.fl, "fr": inner.fr, "rl": inner.rl, "rr": inner.rr},
            "tyre_hint": snapshot.cool_tyre_hint,
            "fuel_laps": snapshot.fuel_remaining_laps,
            "last_hot_ms": snapshot.last_hot.lap_time_ms if snapshot.last_hot else None,
            "mistakes": snapshot.last_hot_mistakes or None,
            "pole": (
                {
                    "driver": snapshot.pole_driver or None,
                    "gap_ms": snapshot.pole_gap_ms,
                    "sector_gaps_ms": list(snapshot.pole_sector_gaps_ms),
                }
                if snapshot.pole_gap_ms > 0
                else None
            ),
            "car_behind_s": _finite(snapshot.hot_car_behind_s),
        }
    return out


def state_payload(
    snapshot: Snapshot,
    *,
    settings: Any,
    metrics: Metrics,
    quiet: bool,
    quiet_left_s: float | None = None,
    silent: bool = False,
    mindset: str | None = None,
    page: str | None = None,
) -> dict[str, Any]:
    cold = settings.thresholds.get("tyre_inner_cold_c", 80.0)
    hot = settings.thresholds.get("tyre_inner_hot_c", 110.0)
    age_ms = packet_age_ms(snapshot)
    live = age_ms is not None and age_ms < STALE_MS

    def corner(name: str) -> dict[str, Any]:
        inner = getattr(snapshot.tyre_inner_ema_fast, name)
        surface = getattr(snapshot.tyre_surface, name)
        wear = getattr(snapshot.tyres_wear, name)
        return {
            "surface": surface,
            "inner": inner,
            "wear": wear,
            "status": tyre_status(inner, cold, hot),
        }

    try:
        from pitwall.protocol.enums import TrackId

        track = TrackId(snapshot.track_id).name.lower()
    except (ValueError, KeyError):
        track = str(snapshot.track_id)

    lat = metrics.summary()
    return {
        "live": live,
        "packet_age_ms": age_ms,
        "rate_hz": None,
        "session_kind": snapshot.session_kind,
        "session_type": snapshot.session_type,
        "track": track,
        "lap_num": snapshot.lap_num,
        "total_laps": snapshot.total_laps,
        "position": snapshot.position,
        "phase": snapshot.phase,
        "tyre_compound": snapshot.tyre_compound,
        "tyre_visual": snapshot.tyre_visual,
        "tyre_age_laps": snapshot.tyre_age_laps,
        "tyres": {
            "fl": corner("fl"),
            "fr": corner("fr"),
            "rl": corner("rl"),
            "rr": corner("rr"),
        },
        "brakes": {
            "fl": snapshot.brake_ema_fast.fl,
            "fr": snapshot.brake_ema_fast.fr,
            "rl": snapshot.brake_ema_fast.rl,
            "rr": snapshot.brake_ema_fast.rr,
        },
        "damage": dataclasses.asdict(snapshot.damage),
        "fuel_remaining_laps": snapshot.fuel_remaining_laps,
        "ers_pct": snapshot.ers_store_pct,
        "safety_car": snapshot.safety_car_status,
        "mindset": mindset or settings.mindset.active,
        "page": page or (settings.ui.pages[0] if settings.ui.pages else "race"),
        "pages": list(settings.ui.pages),
        "verbosity": settings.policy.verbosity,
        "quiet": quiet or quiet_left_s is not None,
        "quiet_left_s": quiet_left_s,
        "silent": silent,
        "red_flag": snapshot.red_flag,
        "paused": snapshot.paused,
        "quali": quali_payload(snapshot, settings.thresholds),
        "race": {
            "phase": snapshot.race_phase,
            "laps_remaining": snapshot.laps_remaining,
            "sc_laps": snapshot.sc_laps,
            "gap_ahead_s": _finite(snapshot.gap_ahead_s),
            "gap_behind_s": _finite(snapshot.gap_behind_s),
            "rival_ahead": {
                "idx": snapshot.rival_ahead_idx,
                "name": snapshot.rival_ahead_name,
                "pace_ms": snapshot.rival_ahead_pace_ms,
                "age": snapshot.rival_ahead_age,
                "pitted": snapshot.rival_ahead_pitted,
            },
            "rival_behind": {
                "idx": snapshot.rival_behind_idx,
                "name": snapshot.rival_behind_name,
                "pace_ms": snapshot.rival_behind_pace_ms,
                "age": snapshot.rival_behind_age,
                "pitted": snapshot.rival_behind_pitted,
            },
            "rival_pit_exit": {
                "idx": snapshot.rival_pit_exit_idx,
                "name": snapshot.rival_pit_exit_name,
                "pace_ms": snapshot.rival_pit_exit_pace_ms,
                "gap_s": _finite(snapshot.pit_exit_rival_gap_s),
            },
            "rival_data_restricted": snapshot.rival_data_restricted,
            "pit_exit_clean": snapshot.pit_exit_clean,
            "laps_of_pace": _finite(snapshot.laps_of_pace),
            "wear_per_lap_pct": snapshot.wear_per_lap_pct,
            "deg_ms_per_lap": snapshot.deg_ms_per_lap,
            "deg_fit_source": snapshot.deg_fit_source,
            "deg_confidence": snapshot.deg_confidence,
            "base_pace_ms": snapshot.base_pace_ms,
            "pit_loss_s": snapshot.pit_loss_s,
            "pit_loss_source": snapshot.pit_loss_source,
            "fuel_margin_laps": _finite(snapshot.fuel_margin_laps),
            "fuel_per_lap_kg": snapshot.fuel_per_lap_kg,
            "fuel_source": snapshot.fuel_source,
            "energy_per_lap_mj": snapshot.energy_per_lap_mj,
            "energy_lap_delta_mj": snapshot.energy_lap_delta_mj,
            "energy_laps_to_floor": _finite(snapshot.energy_laps_to_floor),
            "energy_mode": snapshot.energy_mode,
            "weather_crossover": snapshot.weather_crossover,
            "pit_plan": snapshot.pit_plan,
            "pit_plan_lap": snapshot.pit_plan_lap,
            "pit_plan_gain_s": snapshot.pit_plan_gain_s,
            "pit_plan_confidence": snapshot.pit_plan_confidence,
            "pit_plan_risk": snapshot.pit_plan_risk,
            "pit_plan_rival_idx": snapshot.pit_plan_rival_idx,
            "pit_plan_rival_name": snapshot.pit_plan_rival_name,
            "pit_plan_reason": snapshot.pit_plan_reason,
            "pit_window_start": snapshot.pit_window_start,
            "pit_window_end": snapshot.pit_window_end,
            "undercut_s": snapshot.undercut_s,
            "overcut_s": snapshot.overcut_s,
            "predicted_lap_ms": snapshot.predicted_lap_ms,
        },
        "pit_board": pit_board_payload(snapshot, settings.thresholds),
        "strategy": strategy_payload(snapshot, settings.thresholds),
        "track_info": track_payload(snapshot),
        "setup": setup_payload(snapshot),
        "latency": {
            "trigger_to_speak_p99_ms": lat["trigger_to_speak_ms"]["p99"],
            "packet_to_ws_p99_ms": lat["packet_to_ws_ms"]["p99"],
        },
    }


def create_app(
    hub: Hub,
    settings_store: ConfigStore,
    metrics: Metrics,
    *,
    speaker_name: str = "null",
    latest_snapshot: Any = None,
    on_client_press: Any = None,
    on_client_message: Any = None,
    review: Any = None,
) -> FastAPI:
    """latest_snapshot: callable -> Snapshot for the state broadcaster/snapshot
    frames (defaults to the hub's no-state placeholder). on_client_press:
    callable(down: bool) fed by {"type":"press"} client messages.
    on_client_message: callable(msg) fed by {"type":"mindset"|"page"} messages.
    review:
    optional ReviewController; when present the /api/review/* routes are
    mounted and the hello frame carries review=True."""
    app = FastAPI(title="pitwall")

    def snapshot_now() -> Snapshot:
        if latest_snapshot is not None:
            snap: Snapshot = latest_snapshot()
            return snap
        return Snapshot(now=0.0)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/radio")
    async def radio() -> FileResponse:
        return FileResponse(WEB_DIR / "radio.html")

    @app.get("/api/health")
    async def health() -> JSONResponse:
        extra = hub.health_source() if hub.health_source is not None else {}
        return JSONResponse(
            {
                "config_hash": settings_store.hash,
                "config_error": settings_store.last_error,
                "latency": metrics.summary(),
                "speaker": speaker_name,
                **extra,
            }
        )

    @app.get("/api/config")
    async def config() -> JSONResponse:
        return JSONResponse(settings_store.current().model_dump())

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        settings = settings_store.current()
        await websocket.send_text(
            json.dumps(
                hub.frame(
                    "hello",
                    {
                        "protocol": PROTOCOL_VERSION,
                        "config_hash": settings_store.hash,
                        "mindset": settings.mindset.active,
                        "verbosity": settings.policy.verbosity,
                        "review": review is not None,
                    },
                )
            )
        )
        hub.clients.add(websocket)
        try:
            first = await websocket.receive_json()
            if first.get("type") != "hello" or first.get("v") != PROTOCOL_VERSION:
                await websocket.close(code=4001, reason="protocol version mismatch")
                return
            payload = state_payload(
                snapshot_now(),
                settings=settings_store.current(),
                metrics=metrics,
                quiet=settings_store.current().policy.quiet,
            )
            payload["calls"] = list(hub.recent_calls)
            await websocket.send_text(json.dumps(hub.frame("snapshot", payload)))
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if (
                    isinstance(msg, dict)
                    and msg.get("type") == "press"
                    and on_client_press is not None
                ):
                    on_client_press(bool(msg.get("down")))
                elif (
                    isinstance(msg, dict)
                    and msg.get("type") in ("mindset", "page")
                    and on_client_message is not None
                ):
                    on_client_message(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.clients.discard(websocket)

    if review is not None:

        @app.get("/api/review/status")
        async def review_status() -> JSONResponse:
            return JSONResponse(review.status())

        @app.get("/api/review/timeline")
        async def review_timeline() -> JSONResponse:
            return JSONResponse(review.timeline)

        @app.get("/api/review/grades")
        async def review_grades() -> JSONResponse:
            return JSONResponse(review.grades())

        @app.post("/api/review/play")
        async def review_play() -> JSONResponse:
            return JSONResponse(await review.play())

        @app.post("/api/review/pause")
        async def review_pause() -> JSONResponse:
            return JSONResponse(await review.pause())

        @app.post("/api/review/seek")
        async def review_seek(request: Request) -> JSONResponse:
            body = await request.json()
            return JSONResponse(
                await review.seek(lap=body.get("lap"), offset_us=body.get("offset_us"))
            )

        @app.post("/api/review/grade")
        async def review_grade(request: Request) -> JSONResponse:
            body = await request.json()
            if not body.get("call_id"):
                return JSONResponse({"error": "call_id is required"}, status_code=400)
            uid = 0
            if review.engine is not None and review.engine.state.session_uid:
                uid = review.engine.state.session_uid
            return JSONResponse(
                review.grade(
                    str(body["call_id"]),
                    str(body.get("rule_id", "")),
                    str(body["grade"]),
                    str(body.get("note", "")),
                    session_uid=uid,
                )
            )

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
