"""Ingest: parse headers, count, and dispatch datagrams to handlers.

Every datagram is optionally written to a RecordingWriter BEFORE parsing, so a
recording stays valid even when the parser is wrong.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pitwall.clock import VirtualClock
from pitwall.config.models import Settings
from pitwall.derive import is_synthetic_uid
from pitwall.protocol.header import (
    HEADER_SIZE,
    PACKET_SIZES,
    PacketHeader,
    is_supported,
    parse_header,
)
from pitwall.store.db import Database

if TYPE_CHECKING:
    from pitwall.net.recording import FileHeader

RATE_WINDOW_S = 5.0

PacketHandler = Callable[[PacketHeader, bytes, float], None]


class DatagramRecorder(Protocol):
    def write_datagram(self, recv_time: float, payload: bytes) -> None: ...


class Ingest:
    def __init__(self, recorder: DatagramRecorder | None = None) -> None:
        self.recorder = recorder
        self._handlers: dict[int, list[PacketHandler]] = defaultdict(list)
        self._accepted: dict[int, int] = defaultdict(int)
        self._dropped_size: dict[int, int] = defaultdict(int)
        self._dropped_unsupported = 0
        self._dropped_malformed = 0
        self._arrivals: dict[int, deque[float]] = defaultdict(deque)
        self.last_session_uid: int = 0
        self.raw_datagrams: int = 0
        # Optional payload rewrite applied after recording, before parsing
        # (e.g. `pitwall replay --mask-restricted`).
        self.transform: Callable[[bytes], bytes] | None = None

    def register(self, packet_id: int, handler: PacketHandler) -> None:
        self._handlers[packet_id].append(handler)

    def on_datagram(self, payload: bytes, recv_time: float) -> None:
        self.raw_datagrams += 1
        if self.recorder is not None:
            self.recorder.write_datagram(recv_time, payload)
        if self.transform is not None:
            payload = self.transform(payload)
        if len(payload) < HEADER_SIZE:
            self._dropped_malformed += 1
            return
        try:
            header = parse_header(payload)
        except struct.error:
            self._dropped_malformed += 1
            return
        if not is_supported(header):
            self._dropped_unsupported += 1
            return
        self.last_session_uid = header.session_uid
        expected = PACKET_SIZES.get(header.packet_id)
        if expected is None or len(payload) != expected:
            self._dropped_size[header.packet_id] += 1
            return
        self._accepted[header.packet_id] += 1
        arrivals = self._arrivals[header.packet_id]
        arrivals.append(recv_time)
        while arrivals and recv_time - arrivals[0] > RATE_WINDOW_S:
            arrivals.popleft()
        for handler in self._handlers[header.packet_id]:
            handler(header, payload, recv_time)

    def rate_hz(self, packet_id: int, now: float) -> float:
        """Observed rate over the last RATE_WINDOW_S seconds."""
        arrivals = self._arrivals.get(packet_id)
        if not arrivals:
            return 0.0
        while arrivals and now - arrivals[0] > RATE_WINDOW_S:
            arrivals.popleft()
        return len(arrivals) / RATE_WINDOW_S

    def census(self, now: float | None = None) -> dict[str, Any]:
        """Stats dict for --stats / doctor."""
        packets: dict[str, Any] = {}
        for pid in sorted(set(self._accepted) | set(self._dropped_size) | set(self._arrivals)):
            entry: dict[str, Any] = {
                "accepted": self._accepted.get(pid, 0),
                "dropped_size_mismatch": self._dropped_size.get(pid, 0),
            }
            if now is not None:
                entry["rate_hz"] = round(self.rate_hz(pid, now), 2)
            packets[str(pid)] = entry
        return {
            "raw_datagrams": self.raw_datagrams,
            "packets": packets,
            "dropped_unsupported": self._dropped_unsupported,
            "dropped_malformed": self._dropped_malformed,
            "session_uid": self.last_session_uid,
        }


@dataclass(frozen=True, slots=True)
class IngestResult:
    path: Path
    session_uid: int
    status: Literal["ingested", "digest_only", "skipped", "error"]
    findings: list[str]
    error: str = ""


def expand_paths(paths: Sequence[str]) -> list[Path]:
    """Expand recordings, recursively searched directories, and glob patterns."""
    import glob

    found: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found.update(item.resolve() for item in path.rglob("*.f1bin") if item.is_file())
            found.update(item.resolve() for item in path.rglob("*.f1bin.zst") if item.is_file())
            continue
        if glob.has_magic(str(path)):
            found.update(
                Path(item).resolve()
                for item in glob.glob(str(path), recursive=True)
                if Path(item).is_file()
            )
        elif path.is_file():
            found.add(path.resolve())
    return sorted(found, key=lambda item: str(item))


def _relabel_calls_mode(
    db: Database, uid: int, path: Path, header: FileHeader, calls_mode: str | None
) -> None:
    """An already ingested session keeps its row, but its `calls_mode` may come
    from a header that `header_calls_mode` now reads differently."""
    mode = calls_mode or header_calls_mode(header.metadata)
    session = db.session_row(uid)
    if not mode or session is None or session.get("calls_mode") == mode:
        return
    db.set_session_origin(
        uid,
        started_at=header.wall_clock_start_us / 1_000_000.0,
        recording_path=str(path),
        calls_mode=mode,
        synthetic=bool(header.metadata.get("synthetic")) or is_synthetic_uid(uid),
        derived_from=str(header.metadata.get("derived_from") or ""),
    )


def header_calls_mode(metadata: dict[str, Any]) -> str:
    """`calls_mode` from a recording header. Recorders before the
    `speech_enabled` field spoke every call regardless of `speech.enabled`, so
    their "off" only holds when `policy.quiet` set it."""
    mode = str(metadata.get("calls_mode") or "")
    if mode == "off" and "speech_enabled" not in metadata and not metadata.get("quiet"):
        return "on"
    return mode


def ingest_recordings(
    db: Database,
    paths: Sequence[str],
    settings: Settings,
    *,
    calls_mode: str | None = None,
    out_dir: Path | None = None,
    rules_dir: Path | None = None,
    isolated: bool = False,
) -> list[IngestResult]:
    """Replay or digest a recording batch, isolating errors to each file."""
    from pitwall.digest import DIGEST_VERSION, build_digest
    from pitwall.engine import build_engine, run_replay
    from pitwall.net.recording import RecordingReader

    digest_dir = out_dir or (Path(settings.persistence.path).expanduser().parent / "digests")
    digest_dir.mkdir(parents=True, exist_ok=True)
    results: list[IngestResult] = []
    for path in expand_paths(paths):
        uid = 0
        try:
            with RecordingReader(path) as reader:
                header = reader.header
            uid = header.session_uid
            if uid and db.is_ingested(uid, DIGEST_VERSION):
                _relabel_calls_mode(db, uid, path, header, calls_mode)
                results.append(IngestResult(path, uid, "skipped", []))
                continue
            with db.transaction():
                digest_only = bool(uid and db.session_has_laps(uid))
                if not digest_only:
                    engine = build_engine(
                        clock=VirtualClock(),
                        overrides={"engine": {"heartbeat_s": 0}},
                        rules_dir=rules_dir,
                        isolated=isolated,
                        sinks=[],
                        db=db,
                        decision_log_fp=io.StringIO(),
                        session_started_at=header.wall_clock_start_us / 1_000_000.0,
                    )
                    asyncio.run(run_replay(path, engine))
                    engine.fold_open_stint()
                    uid = engine.state.session_uid or engine.ingest.last_session_uid or uid
                if not uid:
                    raise ValueError("recording did not contain a session UID")
                session = db.session_row(uid) or {}
                origin_mode = calls_mode or header_calls_mode(header.metadata)
                if not origin_mode:
                    origin_mode = str(session.get("calls_mode") or "")
                db.set_session_origin(
                    uid,
                    started_at=header.wall_clock_start_us / 1_000_000.0,
                    recording_path=str(path),
                    calls_mode=origin_mode,
                    synthetic=bool(header.metadata.get("synthetic")) or is_synthetic_uid(uid),
                    derived_from=str(header.metadata.get("derived_from") or ""),
                )
                digest = build_digest(
                    db, uid, settings.thresholds, setup_rules=settings.setup_rules
                )
                (digest_dir / f"{uid}.json").write_text(json.dumps(digest, indent=2, default=str))
                db.mark_ingested(uid, DIGEST_VERSION, str(path))
            findings = [str(item) for item in digest.get("findings", [])]
            results.append(
                IngestResult(
                    path,
                    uid,
                    "digest_only" if digest_only else "ingested",
                    findings,
                )
            )
        except Exception as exc:
            results.append(IngestResult(path, uid, "error", [], str(exc)))
    return results
