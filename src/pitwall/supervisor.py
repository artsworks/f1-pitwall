"""Crash resilience (docs/18 Recovery): the recorder is the supervisor.

`pitwall start` binds the game's UDP port here, writes every datagram to the
recording and forwards it to the engine child on loopback. The recorder starts
first and closes last; the engine child is restarted when it exits non-zero or
stops touching its liveness file, and on start it rebuilds from SQLite plus the
recording tail (`Engine.recover`)."""

from __future__ import annotations

import contextlib
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pitwall.config.models import EngineSettings
from pitwall.net.recording import RecordingRotator

RECORDING_POINTER = "current_recording"
ALIVE_FILE = "engine.alive"
_RECV_TIMEOUT_S = 0.2
_CHECK_PERIOD_S = 0.5
_STOP_WAIT_S = 10.0


def runtime_dir(recordings_dir: Path) -> Path:
    return Path(recordings_dir) / ".runtime"


def read_recording_pointer(rt_dir: Path) -> Path | None:
    try:
        text = (rt_dir / RECORDING_POINTER).read_text().strip()
    except OSError:
        return None
    return Path(text) if text else None


class Supervisor:
    def __init__(
        self,
        engine: EngineSettings,
        recorder: RecordingRotator | None,
        child_cmd: Sequence[str],
        rt_dir: Path,
        *,
        udp_host: str,
        udp_port: int,
        log: Callable[[str], None] = print,
    ) -> None:
        self.engine = engine
        self.recorder = recorder
        self.child_cmd = list(child_cmd)
        self.rt_dir = Path(rt_dir)
        self.rt_dir.mkdir(parents=True, exist_ok=True)
        self.udp_host = udp_host
        self.udp_port = udp_port
        self.log = log
        self.restarts = 0
        self.datagrams = 0
        self.stopping = False
        self._child: subprocess.Popen[bytes] | None = None
        self._spawned_at = 0.0
        self._backoff_s = 0.0
        self._respawn_at: float | None = None
        self._pointer: Path | None = None

    @property
    def alive_path(self) -> Path:
        return self.rt_dir / ALIVE_FILE

    def stop(self) -> None:
        self.stopping = True

    def run(self) -> int:
        """Block until stop() or a clean (exit 0) child exit; returns 0."""
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with contextlib.suppress(OSError):
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        rx.bind((self.udp_host, self.udp_port))
        rx.settimeout(_RECV_TIMEOUT_S)
        target = ("127.0.0.1", self.engine.engine_port)
        self._spawn()
        next_check = time.monotonic() + _CHECK_PERIOD_S
        try:
            while not self.stopping:
                try:
                    payload = rx.recv(65535)
                except TimeoutError:
                    payload = b""
                except OSError:
                    payload = b""  # Windows reports ICMP resets on recv; keep going
                if payload:
                    self.datagrams += 1
                    if self.recorder is not None:
                        self.recorder.write_datagram(time.monotonic(), payload)
                        self._update_pointer()
                    with contextlib.suppress(OSError):
                        tx.sendto(payload, target)
                now = time.monotonic()
                if now >= next_check:
                    next_check = now + _CHECK_PERIOD_S
                    self._check_child(now)
        except KeyboardInterrupt:
            self.stopping = True
        finally:
            self._stop_child()
            rx.close()
            tx.close()
            if self.recorder is not None:
                self.recorder.close()
        return 0

    def _update_pointer(self) -> None:
        assert self.recorder is not None
        path = self.recorder.current_path
        if path is not None and path != self._pointer:
            self._pointer = path
            (self.rt_dir / RECORDING_POINTER).write_text(str(path.resolve()))

    def _spawn(self) -> None:
        with contextlib.suppress(OSError):
            self.alive_path.unlink()
        self._child = subprocess.Popen(self.child_cmd)
        self._spawned_at = time.monotonic()
        self._respawn_at = None

    def _check_child(self, now: float) -> None:
        if self._respawn_at is not None:
            if now >= self._respawn_at:
                self.restarts += 1
                self.log(f"watchdog: restarting engine (restart {self.restarts})")
                self._spawn()
            return
        child = self._child
        if child is None:
            return
        code = child.poll()
        if code is None:
            if self._stalled(now):
                stall = self.engine.watchdog_stall_s
                self.log(f"watchdog: engine stalled > {stall:.0f} s, killing")
                child.kill()
                child.wait()
                code = -1
            else:
                return
        if code == 0:
            self.log("watchdog: engine exited cleanly, stopping")
            self.stopping = True
            return
        ran = now - self._spawned_at
        self._backoff_s = (
            0.0
            if ran > self.engine.watchdog_reset_s
            else min(self.engine.watchdog_backoff_max_s, max(0.5, self._backoff_s * 2))
        )
        self.log(
            f"watchdog: engine died (exit {code}) after {ran:.0f} s; respawn in "
            f"{self._backoff_s:.1f} s, recorder still running"
        )
        self._child = None
        self._respawn_at = now + self._backoff_s

    def _stalled(self, now: float) -> bool:
        if now - self._spawned_at < self.engine.watchdog_grace_s:
            return False
        try:
            age = time.time() - self.alive_path.stat().st_mtime
        except OSError:
            return True
        return age > self.engine.watchdog_stall_s

    def _stop_child(self) -> None:
        child, self._child = self._child, None
        if child is None or child.poll() is not None:
            return
        try:
            child.wait(timeout=_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
