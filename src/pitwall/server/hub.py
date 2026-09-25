"""Hub: WebSocket fan-out and CallSink for the Dispatcher (docs/04).

Envelope {"v":1,"type","seq","t","payload"}. A ring buffer of the last 12
calls backs the reconnect `snapshot` frame.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from pitwall.audio.dispatcher import Call

if TYPE_CHECKING:
    from fastapi import WebSocket

PROTOCOL_VERSION = 1
CALLS_BUFFER = 12


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.seq = 0
        self.recent_calls: deque[dict[str, Any]] = deque(maxlen=CALLS_BUFFER)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Frames captured when no loop is attached (tests, replay pre-server).
        self.outbox: list[dict[str, Any]] = []
        self.health_source: Any = None  # callable -> dict for /api/health

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- framing -----------------------------------------------------------

    def frame(self, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        return {
            "v": PROTOCOL_VERSION,
            "type": type_,
            "seq": self.seq,
            "t": time.time(),
            "payload": payload,
        }

    def broadcast(self, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        frame = self.frame(type_, payload)
        if type_ == "call":
            self.recent_calls.append({"seq": frame["seq"], **payload})
        self._send(frame)
        return frame

    def _send(self, frame: dict[str, Any]) -> None:
        data = json.dumps(frame)
        if self._loop is None:
            self.outbox.append(frame)
            return
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(self._send_ws(ws, data), self._loop)

    async def _send_ws(self, ws: WebSocket, data: str) -> None:
        try:
            await ws.send_text(data)
        except Exception:
            self.clients.discard(ws)

    # -- CallSink ----------------------------------------------------------

    def speak(self, call: Call) -> None:
        self.broadcast(
            "call",
            {
                "id": call.id,
                "rule_id": call.rule_id,
                "priority": call.priority,
                "text": call.text,
                "deadline_ms": call.deadline_ms,
                "tags": call.tags,
                "lap": call.lap,
            },
        )

    def cancel(self, call_id: str) -> None:
        self.broadcast("cancel", {"id": call_id})

    def spoken(self, call_id: str, t: float) -> None:
        self.broadcast("spoken", {"id": call_id, "t": t})
