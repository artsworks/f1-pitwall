"""Watchdog supervisor: the recorder outlives engine crashes and hangs."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

from pitwall.config.models import EngineSettings
from pitwall.net.recording import RecordingReader, RecordingRotator
from pitwall.supervisor import Supervisor, read_recording_pointer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _settings(**kw: float) -> EngineSettings:
    base: dict[str, float] = {
        "engine_port": _free_port(),
        "watchdog_grace_s": 60.0,
        "watchdog_stall_s": 60.0,
        "watchdog_backoff_max_s": 0.1,
    }
    return EngineSettings.model_validate({**base, **kw})


def _run(sup: Supervisor) -> threading.Thread:
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    return t


def _wait(pred, timeout: float = 15.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _datagram(i: int) -> bytes:
    return b"\xea\x07" + bytes(5) + (0x1234).to_bytes(8, "little") + i.to_bytes(4, "little")


def test_crashing_engine_is_restarted_and_recording_continues(tmp_path: Path) -> None:
    port = _free_port()
    rec = RecordingRotator(tmp_path / "rec", profile="full")
    crash = [sys.executable, "-c", "import time, sys; time.sleep(0.2); sys.exit(3)"]
    sup = Supervisor(
        _settings(),
        rec,
        crash,
        tmp_path / "rt",
        udp_host="127.0.0.1",
        udp_port=port,
        log=lambda m: None,
    )
    t = _run(sup)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    end = time.monotonic() + 15
    while sup.restarts < 2 and time.monotonic() < end:
        tx.sendto(_datagram(sent), ("127.0.0.1", port))
        sent += 1
        time.sleep(0.01)
    assert sup.restarts >= 2
    assert _wait(lambda: sup.datagrams == sent, 2.0)
    pointer = read_recording_pointer(tmp_path / "rt")
    sup.stop()
    t.join(15)
    assert not t.is_alive()
    assert pointer is not None and pointer.exists()
    with RecordingReader(pointer) as r:
        assert sum(1 for _ in r) == sent


def test_clean_engine_exit_stops_the_supervisor(tmp_path: Path) -> None:
    ok = [sys.executable, "-c", "pass"]
    sup = Supervisor(
        _settings(),
        None,
        ok,
        tmp_path,
        udp_host="127.0.0.1",
        udp_port=_free_port(),
        log=lambda m: None,
    )
    t = _run(sup)
    t.join(15)
    assert not t.is_alive() and sup.restarts == 0


def test_hung_engine_is_killed_and_restarted(tmp_path: Path) -> None:
    hang = [sys.executable, "-c", "import time; time.sleep(60)"]
    sup = Supervisor(
        _settings(watchdog_grace_s=0.3, watchdog_stall_s=0.3),
        None,
        hang,
        tmp_path,
        udp_host="127.0.0.1",
        udp_port=_free_port(),
        log=lambda m: None,
    )
    t = _run(sup)
    assert _wait(lambda: sup.restarts >= 1)
    sup.stop()
    t.join(15)
    assert not t.is_alive()
