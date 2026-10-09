"""UDP listener with a kernel receive queue to absorb event-loop stalls."""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol

from pitwall.clock import Clock

RCVBUF_CANDIDATES = tuple(size * 1024 * 1024 for size in (32, 16, 8, 4))
RCVBUF_MIN_OK = 4 * 1024 * 1024


class PacketSink(Protocol):
    def on_datagram(self, payload: bytes, recv_time: float) -> None: ...


class UDPListener(asyncio.DatagramProtocol):
    def __init__(self, sink: PacketSink, clock: Clock) -> None:
        self._sink = sink
        self._clock = clock

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._sink.on_datagram(data, self._clock.now())


def effective_rcvbuf(transport: asyncio.DatagramTransport) -> int:
    """Return the socket receive buffer size, or zero when it is unavailable."""
    sock = transport.get_extra_info("socket")
    if sock is None:
        return 0
    try:
        return int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
    except (AttributeError, OSError):
        return 0


async def listen(
    host: str,
    port: int,
    sink: PacketSink,
    clock: Clock,
    *,
    rcvbuf: int | None = None,
) -> asyncio.DatagramTransport:
    """Bind host/port and start dispatching datagrams to sink."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    candidates = (rcvbuf,) if rcvbuf is not None else RCVBUF_CANDIDATES
    for size in candidates:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
            break
        except OSError:
            continue
    sock.bind((host, port))
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UDPListener(sink, clock),
        sock=sock,
    )
    assert isinstance(transport, asyncio.DatagramTransport)
    return transport
