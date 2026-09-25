"""FastAPI app: static dashboard, /api/health, /api/config, WS /ws."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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


def state_payload(
    snapshot: Snapshot,
    *,
    settings: Any,
    metrics: Metrics,
    quiet: bool,
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
        "quiet": quiet,
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
) -> FastAPI:
    """latest_snapshot: callable -> Snapshot for the state broadcaster/snapshot
    frames (defaults to the hub's no-state placeholder)."""
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
                await websocket.receive_text()  # pings/disconnects; no client commands yet
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.clients.discard(websocket)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
