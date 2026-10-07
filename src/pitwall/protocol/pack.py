"""Build protocol packets from the packet layout tables."""

from __future__ import annotations

import struct
from collections.abc import Iterable
from typing import cast

from pitwall.protocol.header import HEADER_STRUCT, PACKET_SIZES
from pitwall.protocol.layouts import Field, Item
from pitwall.protocol.packets import _PACKET_CLASSES, _compiled  # noqa: SLF001


def _field_values(item: Field, value: object) -> list[object]:
    if item.count == 1:
        if value is None:
            return [b"\0" * struct.calcsize("<" + item.fmt) if item.fmt.endswith("s") else 0]
        if isinstance(value, str):
            value = value.encode("utf-8")
        if item.fmt.endswith("s") and not isinstance(value, bytes):
            return [bytes(value)]  # type: ignore[call-overload]
        return [value]
    n = item.count
    if value is None:
        return [0] * n
    seq = list(cast(Iterable[object], value))
    return (seq + [0] * n)[:n]


def _layout_values(layout: tuple[Item, ...], data: dict[str, object]) -> list[object]:
    vals: list[object] = []
    for item in layout:
        if isinstance(item, Field):
            vals.extend(_field_values(item, data.get(item.name)))
        else:
            cars = data.get(item.name, {})
            for i in range(item.n):
                sub = cars.get(i, {}) if isinstance(cars, dict) else {}
                vals.extend(_layout_values(item.layout, sub))
    return vals


def pack_packet(
    packet_id: int,
    data: dict[str, object] | None = None,
    *,
    session_uid: int = 0xDEADBEEF,
    session_time: float = 1.0,
    frame: int = 1,
    player: int = 0,
) -> bytes:
    """Pack a packet from a layout table and field-value mapping."""
    _cls, layout = _PACKET_CLASSES[packet_id]
    compiled = _compiled(layout)
    body = compiled.struct.pack(*_layout_values(layout, data or {}))
    header = HEADER_STRUCT.pack(
        2026, 26, 1, 0, 1, packet_id, session_uid, session_time, frame, frame, player, 255
    )
    pkt = header + body
    assert len(pkt) == PACKET_SIZES[packet_id]
    return pkt
