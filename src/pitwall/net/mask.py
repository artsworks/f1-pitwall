"""Restricted-telemetry simulation (docs/13 staging): zero the rival fields
an online lobby hides, so a single-player recording replays as if online."""

from __future__ import annotations

import struct

from pitwall.protocol.header import HEADER_SIZE, PacketId, parse_header
from pitwall.protocol.packets import car_field_offset

NUM_CARS = 22

# (packet id, field, byte width) hidden for rival cars when a player restricts
# their UDP telemetry: fuel, ERS and tyre wear/damage.
_MASKED: tuple[tuple[int, str, int], ...] = (
    (PacketId.CAR_STATUS, "fuel_in_tank", 4),
    (PacketId.CAR_STATUS, "fuel_capacity", 4),
    (PacketId.CAR_STATUS, "fuel_remaining_laps", 4),
    (PacketId.CAR_STATUS, "ers_store_energy", 4),
    (PacketId.CAR_STATUS, "ers_deploy_mode", 1),
    (PacketId.CAR_STATUS, "ers_harvested_this_lap_mguk", 4),
    (PacketId.CAR_STATUS, "ers_harvested_this_lap_mguh", 4),
    (PacketId.CAR_STATUS, "ers_deployed_this_lap", 4),
    (PacketId.CAR_DAMAGE, "tyres_wear", 16),
    (PacketId.CAR_DAMAGE, "tyres_damage", 4),
    (PacketId.CAR_DAMAGE, "tyre_blisters", 4),
)

_BY_PACKET: dict[int, list[tuple[int, int, int]]] = {}
for _pid, _name, _width in _MASKED:
    for _car in range(NUM_CARS):
        _BY_PACKET.setdefault(int(_pid), []).append(
            (_car, car_field_offset(_pid, _car, _name), _width)
        )


def mask_restricted(payload: bytes) -> bytes:
    """Return payload with every non-player car's restricted fields zeroed.
    Other packets (and malformed datagrams) pass through untouched."""
    if len(payload) < HEADER_SIZE:
        return payload
    try:
        header = parse_header(payload)
    except struct.error:
        return payload
    spans = _BY_PACKET.get(header.packet_id)
    if spans is None:
        return payload
    buf = bytearray(payload)
    player = header.player_car_index
    for car, off, width in spans:
        if car != player and off + width <= len(buf):
            buf[off : off + width] = bytes(width)
    return bytes(buf)
