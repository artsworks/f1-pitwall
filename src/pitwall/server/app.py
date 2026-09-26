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
        "mindset": settings.mindset.active,
        "verbosity": settings.policy.verbosity,
        "quiet": quiet or quiet_left_s is not None,
        "quiet_left_s": quiet_left_s,
        "silent": silent,
        "red_flag": snapshot.red_flag,
        "paused": snapshot.paused,
        "quali": quali_payload(snapshot, settings.thresholds),
        "pit_board": pit_board_payload(snapshot, settings.thresholds),
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
    review: Any = None,
) -> FastAPI:
    """latest_snapshot: callable -> Snapshot for the state broadcaster/snapshot
    frames (defaults to the hub's no-state placeholder). on_client_press:
    callable(down: bool) fed by {"type":"press"} client messages. review:
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
