"""FastAPI app: static dashboard, /api/health, /api/config, WS /ws."""

from __future__ import annotations

import dataclasses
import json
import math
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


def quali_payload(snapshot: Snapshot) -> dict[str, Any] | None:
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
    }
    if snapshot.phase in ("garage", "pitting"):
        out["release"] = {
            "clean": snapshot.release_clean,
            "wait_s": _finite(snapshot.release_wait_s),
            "gap_ahead_s": _finite(snapshot.release_gap_ahead_s),
            "gap_behind_s": _finite(snapshot.release_gap_behind_s),
            "cars_on_track": snapshot.cars_on_track,
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
    return out


def state_payload(
    snapshot: Snapshot,
    *,
    settings: Any,
    metrics: Metrics,
    quiet: bool,
    quiet_left_s: float | None = None,
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
        "red_flag": snapshot.red_flag,
        "paused": snapshot.paused,
        "quali": quali_payload(snapshot),
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
