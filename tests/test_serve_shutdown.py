from __future__ import annotations

import asyncio
import logging
import signal
import socket
import time

import pytest
import uvicorn
from fastapi import FastAPI

from pitwall.server.serve import PitwallServer, serve_with


def _server() -> PitwallServer:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = uvicorn.Config(
        FastAPI(),
        host="127.0.0.1",
        port=port,
        timeout_graceful_shutdown=3,
    )
    return PitwallServer(config)


async def _wait_started(server: PitwallServer) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while not server.started and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert server.started


def test_double_ctrl_c_exits_fast_without_traceback(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    previous_handler = signal.getsignal(signal.SIGINT)
    cancelled: list[bool] = []

    async def run() -> None:
        server = _server()

        async def side() -> None:
            try:
                while True:
                    await asyncio.sleep(0.01)
            finally:
                cancelled.append(True)

        serving = asyncio.create_task(serve_with(server, side()))
        await _wait_started(server)
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
        signal.raise_signal(signal.SIGINT)
        await serving

    caplog.set_level(logging.DEBUG)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run())
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert cancelled
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert "Traceback" not in capsys.readouterr().err
    assert signal.getsignal(signal.SIGINT) == previous_handler


class _FakeTransport:
    def __init__(
        self,
        connection: _StuckConnection,
        connections: set[asyncio.Protocol],
        aborted: list[bool],
    ) -> None:
        self.connection = connection
        self.connections = connections
        self.aborted = aborted

    def abort(self) -> None:
        self.aborted.append(True)
        self.connections.discard(self.connection)


class _StuckConnection(asyncio.Protocol):
    def __init__(self, connections: set[asyncio.Protocol], aborted: list[bool]) -> None:
        self.transport: _FakeTransport = _FakeTransport(self, connections, aborted)

    def shutdown(self) -> None:
        pass


def test_stuck_connection_is_dropped_after_grace(caplog: pytest.LogCaptureFixture) -> None:
    aborted: list[bool] = []

    async def run() -> None:
        server = _server()

        async def side() -> None:
            while True:
                await asyncio.sleep(0.01)

        serving = asyncio.create_task(serve_with(server, side()))
        await _wait_started(server)
        connection = _StuckConnection(server.server_state.connections, aborted)
        server.server_state.connections.add(connection)
        signal.raise_signal(signal.SIGINT)
        await serving

    caplog.set_level(logging.DEBUG)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run())
    elapsed = time.monotonic() - started

    assert aborted
    assert elapsed < 1.5
    assert not any(
        "timeout graceful shutdown exceeded" in record.getMessage() for record in caplog.records
    )


def test_side_task_error_stops_server_and_propagates() -> None:
    async def run() -> None:
        server = _server()
        side_started = asyncio.Event()

        async def side() -> None:
            await side_started.wait()
            raise RuntimeError("boom")

        serving = asyncio.create_task(serve_with(server, side()))
        await _wait_started(server)
        side_started.set()
        await serving

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
