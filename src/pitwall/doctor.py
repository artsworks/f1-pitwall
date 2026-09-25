"""`pitwall doctor`: PASS/WARN/FAIL checks for the game PC setup."""

from __future__ import annotations

import asyncio
import shutil
import socket
import sys
from pathlib import Path
from typing import IO

from pitwall.clock import WallClock
from pitwall.config.loader import ConfigStore
from pitwall.ingest import Ingest
from pitwall.net.udp import listen
from pitwall.protocol.header import PACKET_SIZES, PacketId

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


class _FormatSnoop(Ingest):
    """Counts datagrams by wire format for the format check."""

    def __init__(self) -> None:
        super().__init__()
        self.formats: dict[int, int] = {}
        self.sizes: dict[int, set[int]] = {}
        self.raw = 0
        self.versions: set[tuple[int, int, int]] = set()
        self.butn_seen = False

    def on_datagram(self, payload: bytes, recv_time: float) -> None:
        self.raw += 1
        if len(payload) >= 2:
            fmt = int.from_bytes(payload[:2], "little")
            self.formats[fmt] = self.formats.get(fmt, 0) + 1
        if len(payload) >= 7:
            self.sizes.setdefault(payload[6], set()).add(len(payload))
            self.versions.add((payload[2], payload[3], payload[4]))
        try:
            if len(payload) >= 33 and payload[6] == PacketId.EVENT:
                code = payload[29:33].decode("ascii", errors="replace")
                if code == "BUTN":
                    self.butn_seen = True
        except Exception:
            pass
        super().on_datagram(payload, recv_time)


def _line(out: IO[str], status: str, msg: str) -> int:
    out.write(f"{status:4} {msg}\n")
    return 1 if status == "FAIL" else 0


def run_doctor(
    *,
    seconds: float = 5.0,
    out: IO[str] = sys.stdout,
    store: ConfigStore | None = None,
) -> int:
    store = store or ConfigStore()
    settings = store.current()
    fails = 0

    v = sys.version_info
    if v >= (3, 12):
        fails += _line(out, "PASS", f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        fails += _line(out, "FAIL", f"Python {v.major}.{v.minor} < 3.12")

    # Config loads + hash.
    if store.last_error is None:
        fails += _line(out, "PASS", f"config loads (hash {store.hash})")
    else:
        fails += _line(out, "FAIL", f"config error: {store.last_error}")

    # UDP bind.
    host, port = settings.connection.udp_host, settings.connection.udp_port
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        fails += _line(out, "PASS", f"UDP {host}:{port} free")
        sock.close()
    except OSError as e:
        fails += _line(
            out,
            "FAIL",
            f"cannot bind UDP {host}:{port}: {e} — another telemetry app "
            "(SimHub?) may hold the port",
        )
        return 1

    # HTTP port free.
    hport = settings.connection.http_port
    try:
        ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ts.bind((settings.connection.http_host, hport))
        ts.close()
        fails += _line(out, "PASS", f"TCP {hport} free")
    except OSError:
        fails += _line(out, "WARN", f"TCP {hport} in use — dashboard port taken")

    # Listen.
    ingest = _FormatSnoop()
    clock = WallClock()

    async def collect() -> None:
        transport = await listen(host, port, ingest, clock)
        await asyncio.sleep(seconds)
        transport.close()

    asyncio.run(collect())
    census = ingest.census(now=clock.now())
    total = sum(p["accepted"] for p in census["packets"].values())
    if ingest.raw == 0:
        _line(
            out,
            "WARN",
            f"no datagrams in {seconds:.0f}s — is the game running with UDP Telemetry On?",
        )
    else:
        status = "PASS" if total else "FAIL"
        fails += _line(out, status, f"{ingest.raw} datagrams in {seconds:.0f}s, {total} accepted")
        if total < ingest.raw:
            size_drops = sum(p["dropped_size_mismatch"] for p in census["packets"].values())
            _line(
                out,
                "INFO",
                f"dropped: unsupported={census['dropped_unsupported']} "
                f"malformed={census['dropped_malformed']} "
                f"size_mismatch={size_drops}",
            )
            for pid in sorted(ingest.sizes):
                expected = PACKET_SIZES.get(pid)
                seen = sorted(ingest.sizes[pid])
                if expected is None or seen != [expected]:
                    _line(out, "INFO", f"  packet id {pid}: seen sizes {seen}, expected {expected}")
        non_2026 = {f: n for f, n in ingest.formats.items() if f != 2026}
        if non_2026:
            _line(
                out,
                "WARN",
                f"saw non-2026 formats {non_2026} — set UDP Format to 2026 in Telemetry Settings",
            )
        else:
            _line(out, "PASS", "format 2026 only")
        vers = ", ".join(f"year {y} v{a}.{b:02d}" for y, a, b in sorted(ingest.versions))
        _line(out, "INFO", f"game reports: {vers}")
        menu_ids = [PacketId.LAP_DATA, PacketId.CAR_TELEMETRY, PacketId.CAR_STATUS]
        rates = [ingest.rate_hz(pid, clock.now()) for pid in menu_ids]
        observed = max(rates) if rates else 0.0
        expected = settings.connection.send_rate_hz
        if observed and abs(observed - expected) / expected > 0.2:
            _line(
                out,
                "WARN",
                f"menu rate {observed:.0f} Hz vs configured {expected} Hz — check UDP Send Rate",
            )
        elif observed:
            _line(out, "PASS", f"menu rate {observed:.0f} Hz")
        if ingest.butn_seen:
            _line(out, "INFO", "BUTN event seen — UDP Action binding works")

    # Speech.
    if sys.platform == "win32":
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            voices = win32com.client.Dispatch("SAPI.SpVoice").GetVoices()
            names = [v.GetDescription() for v in voices]
            _line(
                out,
                "PASS",
                f"SAPI available ({len(names)} voices: "
                f"{', '.join(names[:4])}{'…' if len(names) > 4 else ''})",
            )
        except Exception as e:
            _line(out, "WARN", f"SAPI unavailable: {e}")
        _line(out, "INFO", "firewall hint:")
        _line(
            out,
            "INFO",
            f'  netsh advfirewall firewall add rule name="pitwall udp" '
            f"dir=in action=allow protocol=UDP localport={port}",
        )
        _line(
            out,
            "INFO",
            f'  netsh advfirewall firewall add rule name="pitwall tcp" '
            f"dir=in action=allow protocol=TCP localport={hport}",
        )

    # Disk free in recording dir.
    rec_dir = Path(settings.recording.directory)
    rec_dir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(rec_dir).free / 1e9
    if free_gb > 5:
        _line(out, "PASS", f"{free_gb:.0f} GB free in {rec_dir}")
    else:
        _line(out, "WARN", f"only {free_gb:.1f} GB free in {rec_dir}")

    out.write("doctor: %s\n" % ("FAIL" if fails else "OK"))
    return 1 if fails else 0


def set_below_normal_priority() -> None:
    """Drop the process to below-normal priority on Windows (docs/09)."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
