"""UDP listener: asyncio.DatagramProtocol dispatching datagrams to a PacketSink."""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol

from pitwall.clock import Clock

RCVBUF_BYTES = 4 * 1024 * 1024


class PacketSink(Protocol):
    def on_datagram(self, payload: bytes, recv_time: float) -> None: ...


class UDPListener(asyncio.DatagramProtocol):
    def __init__(self, sink: PacketSink, clock: Clock) -> None:
        self._sink = sink
        self._clock = clock

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._sink.on_datagram(bytes(data), self._clock.now())


async def listen(host: str, port: int, sink: PacketSink, clock: Clock) -> asyncio.DatagramTransport:
    """Bind host/port and start dispatching datagrams to sink."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
    except OSError:
        pass  # large buffer is best-effort
    sock.bind((host, port))
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UDPListener(sink, clock),
        sock=sock,
    )
    assert isinstance(transport, asyncio.DatagramTransport)
    return transport
