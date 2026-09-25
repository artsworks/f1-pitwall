""".f1bin recording writer/reader and .f1idx sidecar index.

File layout (docs/07-replay-and-debug.md), all little-endian:

    header:  magic "F1BIN\\0" | uint16 file_version | uint16 packet_format
             uint64 session_uid | uint64 wall_clock_start_us
             uint32 config_hash | uint16 game_version | uint16 reserved
             uint32 json_len | utf8 json blob (host, settings, notes)
    record:  uint32 delta_us (since previous record) | uint16 length | payload

The .f1idx sidecar is a JSON list of {kind, byte_offset, offset_us, detail}:
- "event" for every Event packet (detail = 4-char event code)
- "session_type" for every Session packet (detail = session type id)
- "lap" when the player's current_lap_num changes (detail = new lap number)
- "pit_in"/"pit_out" on the player's pit_status 0->1 / 2->0 transitions
"""

from __future__ import annotations

import io
import json
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import zstandard

from pitwall.net.profile import ProfileName, RecordFilter
from pitwall.protocol.header import (
    EVENT_CODE_LEN,
    EVENT_CODE_OFFSET,
    PACKET_ID_OFFSET,
    PACKET_SIZES,
    PLAYER_CAR_INDEX_OFFSET,
    PacketId,
)

MAGIC = b"F1BIN\0"
FILE_VERSION = 1

# magic | file_version | packet_format | session_uid | wall_clock_start_us |
# config_hash | game_version | reserved
FILE_HEADER_STRUCT = struct.Struct("<6sHHQQIHH")
# uint32 json_len, then the utf-8 blob
JSON_LEN_STRUCT = struct.Struct("<I")
# uint32 delta_us (since previous record) | uint16 length
RECORD_HEADER_STRUCT = struct.Struct("<IH")

FLUSH_INTERVAL_S = 1.0
ZSTD_SUFFIX = ".zst"
IDX_SUFFIX = ".f1idx"


@dataclass(frozen=True, slots=True)
class FileHeader:
    file_version: int
    packet_format: int
    session_uid: int
    wall_clock_start_us: int
    config_hash: int
    game_version: int
    metadata: dict[str, Any]


@dataclass(slots=True)
class IndexEntry:
    kind: str  # "lap" | "session_type" | "pit_in" | "pit_out" | "event"
    byte_offset: int
    offset_us: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "byte_offset": self.byte_offset,
            "offset_us": self.offset_us,
            "detail": self.detail,
        }


@dataclass(slots=True)
class _IndexState:
    last_lap: int | None = None
    last_pit: int | None = None


def _index_for_record(
    payload: bytes, byte_offset: int, offset_us: int, state: _IndexState
) -> list[IndexEntry]:
    """Index entries derivable from one record without full parsing."""
    from pitwall.protocol.packets import SESSION_TYPE_OFFSET, car_field_offset

    if len(payload) < EVENT_CODE_OFFSET + EVENT_CODE_LEN:
        return []
    pid = payload[PACKET_ID_OFFSET]
    entries: list[IndexEntry] = []
    if pid == PacketId.EVENT:
        code = payload[EVENT_CODE_OFFSET : EVENT_CODE_OFFSET + EVENT_CODE_LEN]
        entries.append(
            IndexEntry("event", byte_offset, offset_us, code.decode("ascii", errors="replace"))
        )
    elif pid == PacketId.SESSION and len(payload) == PACKET_SIZES[PacketId.SESSION]:
        entries.append(
            IndexEntry("session_type", byte_offset, offset_us, str(payload[SESSION_TYPE_OFFSET]))
        )
    elif pid == PacketId.LAP_DATA and len(payload) == PACKET_SIZES[PacketId.LAP_DATA]:
        player = payload[PLAYER_CAR_INDEX_OFFSET]
        lap = payload[car_field_offset(PacketId.LAP_DATA, player, "current_lap_num")]
        pit = payload[car_field_offset(PacketId.LAP_DATA, player, "pit_status")]
        if state.last_lap is not None and lap != state.last_lap:
            entries.append(IndexEntry("lap", byte_offset, offset_us, str(lap)))
        if state.last_pit is not None:
            if state.last_pit == 0 and pit == 1:
                entries.append(IndexEntry("pit_in", byte_offset, offset_us, "in"))
            elif state.last_pit == 2 and pit == 0:
                entries.append(IndexEntry("pit_out", byte_offset, offset_us, "out"))
        state.last_lap = lap
        state.last_pit = pit
    return entries


def _open_maybe_zst(path: Path) -> IO[bytes]:
    raw = path.open("rb")
    if path.suffix == ZSTD_SUFFIX:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        return io.BufferedReader(reader)
    return raw


def index_path_for(path: Path) -> Path:
    p = Path(path)
    name = p.name
    for suffix in (ZSTD_SUFFIX, ".f1bin"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return p.with_name(name + IDX_SUFFIX)


class RecordingWriter:
    """Buffered .f1bin writer. Flushes on a timer, not per datagram."""

    def __init__(
        self,
        path: Path,
        *,
        packet_format: int = 2026,
        session_uid: int = 0,
        config_hash: int = 0,
        game_version: int = 0,
        metadata: dict[str, Any] | None = None,
        wall_clock_start_us: int | None = None,
        flush_interval_s: float = FLUSH_INTERVAL_S,
    ) -> None:
        self.path = Path(path)
        self.session_uid = session_uid
        self.wall_clock_start_us = (
            wall_clock_start_us if wall_clock_start_us is not None else time.time_ns() // 1000
        )
        self._file: IO[bytes] = io.BufferedWriter(self.path.open("wb"))
        blob = json.dumps(metadata or {}).encode("utf-8")
        self._file.write(
            FILE_HEADER_STRUCT.pack(
                MAGIC,
                FILE_VERSION,
                packet_format,
                session_uid,
                self.wall_clock_start_us,
                config_hash,
                game_version,
                0,
            )
        )
        self._file.write(JSON_LEN_STRUCT.pack(len(blob)))
        self._file.write(blob)
        self._flush_interval_s = flush_interval_s
        self._last_flush = time.monotonic()
        self._t0: float | None = None
        self._last_offset_us = 0
        self._index: list[IndexEntry] = []
        self._idx_state = _IndexState()
        self._closed = False

    def write_datagram(self, recv_time: float, payload: bytes) -> None:
        """Append one datagram. recv_time is a monotonic timestamp in seconds."""
        if self._closed:
            raise ValueError("writer is closed")
        if self._t0 is None:
            self._t0 = recv_time
        offset_us = int((recv_time - self._t0) * 1_000_000)
        # Stored as a delta from the previous record so a uint32 covers a full
        # race (absolute offsets would wrap at ~71.6 min).
        delta_us = offset_us - self._last_offset_us
        self._last_offset_us = offset_us
        byte_offset = self._file.tell()
        # A single uint32 delta can't span gaps longer than ~71.6 min; bridge
        # them with zero-length filler records.
        while delta_us > 0xFFFFFFFF:
            self._file.write(RECORD_HEADER_STRUCT.pack(0xFFFFFFFF, 0))
            delta_us -= 0xFFFFFFFF
        self._file.write(RECORD_HEADER_STRUCT.pack(delta_us, len(payload)))
        self._file.write(payload)
        self._maybe_index(payload, byte_offset, offset_us)
        now = time.monotonic()
        if now - self._last_flush >= self._flush_interval_s:
            self._file.flush()
            self._last_flush = now

    def _maybe_index(self, payload: bytes, byte_offset: int, offset_us: int) -> None:
        self._index.extend(_index_for_record(payload, byte_offset, offset_us, self._idx_state))

    def flush(self) -> None:
        self._file.flush()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._file.flush()
        self._file.close()
        write_index(index_path_for(self.path), self._index)

    def __enter__(self) -> RecordingWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def write_index(path: Path, entries: list[IndexEntry]) -> None:
    path.write_text(json.dumps([e.to_dict() for e in entries]))


class RecordingRotator:
    """PacketSink-shaped writer that starts a new .f1bin file whenever the
    session UID changes (docs: "a new file per session UID").

    `profile` selects which datagrams are written (pitwall.net.profile). With
    `compress`, each finished file is replaced by its .f1bin.zst: rotated files
    in a background thread, the last one synchronously on close()."""

    def __init__(
        self,
        directory: Path,
        *,
        packet_format: int = 2026,
        config_hash: int = 0,
        game_version: int = 0,
        metadata: dict[str, Any] | None = None,
        profile: ProfileName = "full",
        compress: bool = False,
    ) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._kwargs: dict[str, Any] = {
            "packet_format": packet_format,
            "config_hash": config_hash,
            "game_version": game_version,
            "metadata": {**(metadata or {}), "profile": profile},
        }
        self._filter = RecordFilter(profile)
        self._compress = compress
        self._compressors: list[threading.Thread] = []
        self._writer: RecordingWriter | None = None
        self._uid: int = 0
        self.last_path: Path | None = None

    @property
    def current_path(self) -> Path | None:
        return self._writer.path if self._writer else None

    def write_datagram(self, recv_time: float, payload: bytes) -> None:
        uid = struct.unpack_from("<Q", payload, 7)[0] if len(payload) >= 15 else self._uid
        if self._writer is None or uid != self._uid:
            self._rotate(uid)
        assert self._writer is not None
        if self._filter.keep(recv_time, payload):
            self._writer.write_datagram(recv_time, payload)

    def _finish(self, background: bool) -> None:
        if self._writer is None:
            return
        self._writer.close()
        path = self._writer.path
        self._writer = None
        self.last_path = path
        if not self._compress:
            return
        if background:
            t = threading.Thread(target=self._compress_file, args=(path,), name="pitwall-zstd")
            t.start()
            self._compressors.append(t)
        else:
            self._compress_file(path)

    def _compress_file(self, path: Path) -> None:
        self.last_path = compress_recording(path, remove=True)

    def _rotate(self, uid: int) -> None:
        self._finish(background=True)
        name = f"session_{uid:016x}_{int(time.time())}.f1bin"
        self._writer = RecordingWriter(self._directory / name, session_uid=uid, **self._kwargs)
        self._uid = uid

    def close(self) -> None:
        self._finish(background=False)
        for t in self._compressors:
            t.join()
        self._compressors.clear()


class RecordingReader:
    """Iterates a .f1bin (or .f1bin.zst) recording as (offset_us, payload)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file = _open_maybe_zst(self.path)
        self.header = self._read_header()
        self._record_base = (
            FILE_HEADER_STRUCT.size
            + JSON_LEN_STRUCT.size
            + len(json.dumps(self.header.metadata).encode("utf-8"))
        )

    def _read_header(self) -> FileHeader:
        raw = self._file.read(FILE_HEADER_STRUCT.size)
        if len(raw) != FILE_HEADER_STRUCT.size:
            raise ValueError(f"{self.path}: truncated file header")
        magic, file_version, packet_format, session_uid, wall_us, config_hash, game_version, _ = (
            FILE_HEADER_STRUCT.unpack(raw)
        )
        if magic != MAGIC:
            raise ValueError(f"{self.path}: bad magic {magic!r}")
        if file_version != FILE_VERSION:
            raise ValueError(f"{self.path}: unsupported file version {file_version}")
        (json_len,) = JSON_LEN_STRUCT.unpack(self._file.read(JSON_LEN_STRUCT.size))
        metadata = json.loads(self._file.read(json_len) or b"{}")
        return FileHeader(
            file_version=file_version,
            packet_format=packet_format,
            session_uid=session_uid,
            wall_clock_start_us=wall_us,
            config_hash=config_hash,
            game_version=game_version,
            metadata=metadata,
        )

    def records(self) -> Iterator[tuple[int, bytes]]:
        """Yield (offset_us, payload) for each record."""
        offset_us = 0
        while True:
            raw = self._file.read(RECORD_HEADER_STRUCT.size)
            if not raw:
                return
            if len(raw) != RECORD_HEADER_STRUCT.size:
                raise ValueError(f"{self.path}: truncated record header")
            delta_us, length = RECORD_HEADER_STRUCT.unpack(raw)
            offset_us += delta_us
            if length == 0:
                continue  # filler record bridging a >71.6 min gap
            payload = self._file.read(length)
            if len(payload) != length:
                raise ValueError(f"{self.path}: truncated record payload")
            yield offset_us, payload

    def __iter__(self) -> Iterator[tuple[int, bytes]]:
        return self.records()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> RecordingReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build_index(path: Path) -> list[IndexEntry]:
    """Rebuild the .f1idx for a recording by scanning its records."""
    entries: list[IndexEntry] = []
    state = _IndexState()
    with RecordingReader(path) as reader:
        byte_offset = reader._record_base  # noqa: SLF001 — same module
        for offset_us, payload in reader:
            entries.extend(_index_for_record(payload, byte_offset, offset_us, state))
            byte_offset += RECORD_HEADER_STRUCT.size + len(payload)
    return entries


def read_index(path: Path) -> list[dict[str, Any]]:
    """Load a .f1idx sidecar if present; returns [] otherwise."""
    idx = index_path_for(path)
    if not idx.exists():
        return []
    data = json.loads(idx.read_text())
    return list(data)


def compress_recording(path: Path, *, level: int = 3, remove: bool = False) -> Path:
    """Compress a .f1bin to .f1bin.zst. Intended for post-session, low priority."""
    src = Path(path)
    dst = src.with_name(src.name + ZSTD_SUFFIX)
    cctx = zstandard.ZstdCompressor(level=level)
    with src.open("rb") as fin, dst.open("wb") as fout, cctx.stream_writer(fout) as zout:
        while chunk := fin.read(1 << 20):
            zout.write(chunk)
    if remove:
        src.unlink()
    return dst
