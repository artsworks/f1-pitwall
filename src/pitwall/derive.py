"""Byte-level recording mutations for derived synthetic sessions."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pitwall.net.recording import (
    FileHeader,
    RecordingReader,
    RecordingWriter,
    compress_recording,
)
from pitwall.protocol.header import (
    HEADER_SIZE,
    PACKET_ID_OFFSET,
    PACKET_SIZES,
    PLAYER_CAR_INDEX_OFFSET,
    SESSION_UID_OFFSET,
    PacketHeader,
    PacketId,
    parse_header,
)
from pitwall.protocol.packets import SESSION_SAFETY_CAR_STATUS_OFFSET, car_field_offset

SYNTHETIC_UID_TAG = 0xF1DE57
_TAG_SHIFT = 40
_NUM_CARS = 22


def derived_uid(source_uid: int, mutations: Sequence[str]) -> int:
    """Return a stable synthetic UID for a source session and mutation list."""
    digest = hashlib.blake2b(
        f"{source_uid}:{json.dumps(list(mutations))}".encode(),
        digest_size=5,
    ).digest()
    return (SYNTHETIC_UID_TAG << _TAG_SHIFT) | int.from_bytes(digest, "big")


def is_synthetic_uid(uid: int) -> bool:
    """Check the synthetic UID tag, including SQLite's signed representation."""
    value = uid & 0xFFFF_FFFF_FFFF_FFFF
    return (value >> _TAG_SHIFT) == SYNTHETIC_UID_TAG


@dataclass(slots=True)
class DeriveContext:
    player_lap: int | None = None
    safety_events: dict[tuple[int, int, bool], set[str]] = field(default_factory=dict)
    penalty_emitted: set[int] = field(default_factory=set)
    penalty_seconds: dict[int, int] = field(default_factory=dict)


class MutationOp(Protocol):
    label: str

    def apply(self, payload: bytes, ctx: DeriveContext) -> list[bytes]: ...


def _header(payload: bytes) -> PacketHeader | None:
    if len(payload) < HEADER_SIZE:
        return None
    try:
        return parse_header(payload)
    except struct.error:
        return None


def _event_from(payload: bytes, code: bytes, detail: bytes) -> bytes:
    event = bytearray(payload[:HEADER_SIZE])
    event[PACKET_ID_OFFSET] = PacketId.EVENT
    body = code + detail
    size = PACKET_SIZES[PacketId.EVENT] - HEADER_SIZE
    return bytes(event) + body.ljust(size, b"\0")[:size]


def _update_player_lap(payload: bytes, ctx: DeriveContext) -> None:
    header = _header(payload)
    if (
        header is None
        or header.packet_id != PacketId.LAP_DATA
        or len(payload) != PACKET_SIZES[PacketId.LAP_DATA]
    ):
        return
    player = payload[PLAYER_CAR_INDEX_OFFSET]
    ctx.player_lap = payload[car_field_offset(PacketId.LAP_DATA, player, "current_lap_num")]


@dataclass(slots=True)
class UidRewrite:
    source_uid: int
    new_uid: int
    label: str = "uid_rewrite"

    def apply(self, payload: bytes, ctx: DeriveContext) -> list[bytes]:
        if len(payload) < HEADER_SIZE:
            return [payload]
        header = _header(payload)
        if (
            header is None
            or header.session_uid != self.source_uid
            or header.packet_id not in PACKET_SIZES
            or len(payload) != PACKET_SIZES[header.packet_id]
        ):
            return [payload]
        rewritten = bytearray(payload)
        rewritten[SESSION_UID_OFFSET : SESSION_UID_OFFSET + 8] = self.new_uid.to_bytes(8, "little")
        return [bytes(rewritten)]


@dataclass(slots=True)
class WearScale:
    factor: float
    label: str = field(init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.factor) or self.factor < 0:
            raise ValueError("wear scale factor must be finite and non-negative")
        self.label = f"wear_scale={self.factor:g}"

    def apply(self, payload: bytes, ctx: DeriveContext) -> list[bytes]:
        header = _header(payload)
        if (
            header is None
            or header.packet_id != PacketId.CAR_DAMAGE
            or len(payload) != PACKET_SIZES[PacketId.CAR_DAMAGE]
        ):
            return [payload]
        rewritten = bytearray(payload)
        for car in range(_NUM_CARS):
            wear = car_field_offset(PacketId.CAR_DAMAGE, car, "tyres_wear")
            damage = car_field_offset(PacketId.CAR_DAMAGE, car, "tyres_damage")
            for corner in range(4):
                offset = wear + corner * 4
                value = struct.unpack_from("<f", rewritten, offset)[0] * self.factor
                if math.isnan(value) or value == -math.inf:
                    value = 0.0
                elif value == math.inf:
                    value = 100.0
                else:
                    value = min(100.0, max(0.0, value))
                struct.pack_into("<f", rewritten, offset, value)
                damage_offset = damage + corner
                scaled = round(rewritten[damage_offset] * self.factor)
                rewritten[damage_offset] = min(100, max(0, scaled))
        return [bytes(rewritten)]


@dataclass(slots=True)
class InjectSafetyCar:
    start_lap: int
    end_lap: int
    vsc: bool = False
    lap_time_factor: float = 1.4
    label: str = field(init=False)

    def __post_init__(self) -> None:
        self.label = f"inject_sc={self.start_lap}-{self.end_lap}" + (":vsc" if self.vsc else "")

    def apply(self, payload: bytes, ctx: DeriveContext) -> list[bytes]:
        header = _header(payload)
        if (
            header is None
            or header.packet_id not in PACKET_SIZES
            or len(payload) != PACKET_SIZES[header.packet_id]
        ):
            return [payload]
        rewritten = bytearray(payload)
        extra: list[bytes] = []
        status = ctx.safety_events.setdefault((self.start_lap, self.end_lap, self.vsc), set())
        if ctx.player_lap is not None and ctx.player_lap >= self.start_lap:
            if "deployed" not in status:
                status.add("deployed")
                extra.append(
                    _event_from(
                        payload,
                        b"SCAR",
                        struct.pack("<BB", 2 if self.vsc else 1, 0),
                    )
                )
            if ctx.player_lap == self.end_lap and "returning" not in status:
                status.add("returning")
                extra.append(
                    _event_from(
                        payload,
                        b"SCAR",
                        struct.pack("<BB", 2 if self.vsc else 1, 1),
                    )
                )
            if ctx.player_lap > self.end_lap and "returned" not in status:
                status.add("returned")
                extra.append(
                    _event_from(
                        payload,
                        b"SCAR",
                        struct.pack("<BB", 2 if self.vsc else 1, 2),
                    )
                )
        if (
            header.packet_id == PacketId.SESSION
            and len(payload) == PACKET_SIZES[PacketId.SESSION]
            and ctx.player_lap is not None
            and self.start_lap <= ctx.player_lap <= self.end_lap
        ):
            rewritten[SESSION_SAFETY_CAR_STATUS_OFFSET] = 2 if self.vsc else 1
        elif (
            header.packet_id == PacketId.LAP_DATA
            and len(payload) == PACKET_SIZES[PacketId.LAP_DATA]
        ):
            for car in range(_NUM_CARS):
                lap_offset = car_field_offset(PacketId.LAP_DATA, car, "current_lap_num")
                current_lap = (
                    ctx.player_lap
                    if car == header.player_car_index and ctx.player_lap is not None
                    else rewritten[lap_offset]
                )
                completed_lap = current_lap - 1
                if not self.start_lap <= completed_lap <= self.end_lap:
                    continue
                time_offset = car_field_offset(PacketId.LAP_DATA, car, "last_lap_time_ms")
                lap_time = struct.unpack_from("<I", rewritten, time_offset)[0]
                if lap_time:
                    scaled = round(lap_time * self.lap_time_factor)
                    struct.pack_into("<I", rewritten, time_offset, min(0xFFFF_FFFF, scaled))
        return [bytes(rewritten), *extra]


@dataclass(slots=True)
class Penalty:
    lap: int
    seconds: int = 5
    label: str = field(init=False)

    def __post_init__(self) -> None:
        self.label = f"penalty={self.lap}"
        if not 0 <= self.seconds <= 255 or not 0 <= self.lap <= 255:
            raise ValueError("penalty seconds and lap must fit in one byte")

    def apply(self, payload: bytes, ctx: DeriveContext) -> list[bytes]:
        header = _header(payload)
        if (
            header is None
            or header.packet_id != PacketId.LAP_DATA
            or len(payload) != PACKET_SIZES[PacketId.LAP_DATA]
        ):
            return [payload]
        player = header.player_car_index
        if ctx.player_lap != self.lap:
            if self.lap in ctx.penalty_seconds:
                rewritten = bytearray(payload)
                self._add_penalty(rewritten, player, ctx.penalty_seconds[self.lap])
                return [bytes(rewritten)]
            return [payload]
        if self.lap not in ctx.penalty_emitted:
            ctx.penalty_emitted.add(self.lap)
            ctx.penalty_seconds[self.lap] = self.seconds
            rewritten = bytearray(payload)
            self._add_penalty(rewritten, player, self.seconds)
            event = _event_from(
                payload,
                b"PENA",
                struct.pack("<BBBBBBB", 4, 7, player, 255, self.seconds, self.lap, 0),
            )
            return [bytes(rewritten), event]
        rewritten = bytearray(payload)
        self._add_penalty(rewritten, player, self.seconds)
        return [bytes(rewritten)]

    @staticmethod
    def _add_penalty(payload: bytearray, player: int, seconds: int) -> None:
        offset = car_field_offset(PacketId.LAP_DATA, player, "penalties")
        payload[offset] = min(255, payload[offset] + seconds)


def derive_records(
    records: Iterable[tuple[int, bytes]],
    ops: Sequence[MutationOp],
    *,
    ctx: DeriveContext | None = None,
) -> Iterator[tuple[int, bytes]]:
    context = ctx or DeriveContext()
    ordered = [op for op in ops if isinstance(op, UidRewrite)] + [
        op for op in ops if not isinstance(op, UidRewrite)
    ]
    for offset_us, payload in records:
        _update_player_lap(payload, context)
        current = [payload]
        for op in ordered:
            current = [result for item in current for result in op.apply(item, context)]
        yield from ((offset_us, item) for item in current)


@dataclass(frozen=True, slots=True)
class DerivedRecordingSummary:
    header: FileHeader
    record_count: int


def derive_recording(src: Path, out: Path, ops: Sequence[MutationOp]) -> DerivedRecordingSummary:
    source = Path(src)
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_path = destination.with_suffix("") if destination.suffix == ".zst" else destination
    with RecordingReader(source) as reader:
        source_header = reader.header
        source_uid = source_header.session_uid
        if source_uid == 0:
            raise ValueError(f"{source}: cannot derive a recording with session UID 0")
        labels = [op.label for op in ops if not isinstance(op, UidRewrite)]
        new_uid = derived_uid(source_uid, labels)
        metadata = {
            **source_header.metadata,
            "synthetic": True,
            "derived_from": str(source_uid),
            "derived_from_path": str(source),
            "mutations": labels,
        }
        rewrite = UidRewrite(source_uid, new_uid)
        count = 0
        with RecordingWriter(
            write_path,
            packet_format=source_header.packet_format,
            session_uid=new_uid,
            config_hash=source_header.config_hash,
            game_version=source_header.game_version,
            wall_clock_start_us=source_header.wall_clock_start_us,
            metadata=metadata,
        ) as writer:
            for offset_us, payload in derive_records(reader.records(), [rewrite, *ops]):
                writer.write_datagram(offset_us / 1_000_000.0, payload)
                count += 1
        if write_path != destination:
            compress_recording(write_path, remove=True)
        header = FileHeader(
            file_version=source_header.file_version,
            packet_format=source_header.packet_format,
            session_uid=new_uid,
            wall_clock_start_us=source_header.wall_clock_start_us,
            config_hash=source_header.config_hash,
            game_version=source_header.game_version,
            metadata=metadata,
        )
    return DerivedRecordingSummary(header=header, record_count=count)
