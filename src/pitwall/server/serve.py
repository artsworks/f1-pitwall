"""Uvicorn server shutdown handling."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import threading
from collections.abc import Coroutine, Generator
from types import FrameType
from typing import Any

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

CLOSE_GRACE_S = 0.5


class PitwallServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.signalled = False
        self._side: list[asyncio.Task[Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # Match uvicorn's handler setup without re-raising signals on exit.
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.signalled:
            self.force_exit = True
        else:
            print("shutting down... (Ctrl+C again to force quit)", flush=True)
        self.signalled = True
        self.should_exit = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_side)

    def _cancel_side(self) -> None:
        for task in self._side:
            task.cancel()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._cancel_side()
        timer = asyncio.get_running_loop().call_later(CLOSE_GRACE_S, self._abort_connections)
        try:
            await super().shutdown(sockets)
        finally:
            timer.cancel()
        shutdown_event = getattr(self.lifespan, "shutdown_event", None)
        if self.force_exit and (shutdown_event is None or not shutdown_event.is_set()):
            await self.lifespan.shutdown()

    def _abort_connections(self) -> None:
        for connection in list(self.server_state.connections):
            transport = getattr(connection, "transport", None)
            if transport is not None:
                transport.abort()

    async def _wait_tasks_to_complete(self) -> None:
        servers, self.servers = self.servers, []
        try:
            await super()._wait_tasks_to_complete()
        finally:
            self.servers = servers
        # asyncio counts TLS handshakes that uvicorn never tracked.
        # Do not wait out the graceful timeout for them.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(server.wait_closed() for server in servers)), CLOSE_GRACE_S
            )


async def serve_with(server: PitwallServer, *coros: Coroutine[Any, Any, Any]) -> None:
    """Serve with coroutines alongside the dashboard server."""
    server._loop = asyncio.get_running_loop()
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    server._side = tasks

    def stop_on_error(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception() is not None:
            server.should_exit = True

    for task in tasks:
        task.add_done_callback(stop_on_error)
    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            raise result
    if server.signalled:
        raise KeyboardInterrupt
