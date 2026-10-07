"""Start Pitwall, send telemetry, and check live health and shutdown."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from pitwall.protocol.header import HEADER_STRUCT, PACKET_SIZES, PacketId


def _free_port(sock_type: int) -> int:
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _packet(packet_id: PacketId, session_time: float, frame: int) -> bytes:
    header = HEADER_STRUCT.pack(
        2026,
        26,
        1,
        0,
        1,
        packet_id,
        0xDEADBEEF,
        session_time,
        frame,
        frame,
        0,
        255,
    )
    body_len = PACKET_SIZES[packet_id] - HEADER_STRUCT.size
    return header + bytes(body_len)


def _health(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _stop_child(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    else:
        proc.wait()


def main() -> int:
    started = time.monotonic()
    output: list[str] = []
    proc: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    failure: str | None = None
    ready_elapsed = 0.0
    telemetry_elapsed = 0.0
    shutdown_elapsed = 0.0

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            udp_port = _free_port(socket.SOCK_DGRAM)
            http_port = _free_port(socket.SOCK_STREAM)
            profile_path = Path(temp_dir) / "profile.yaml"
            profile = {
                "connection": {
                    "udp_host": "127.0.0.1",
                    "udp_port": udp_port,
                    "http_host": "127.0.0.1",
                    "http_port": http_port,
                    "require_pin": False,
                },
                "recording": {
                    "enabled": False,
                    "directory": str(Path(temp_dir) / "recordings"),
                },
                "persistence": {"enabled": False},
                "speech": {"enabled": False},
                "engine": {"watchdog": False},
                "learning": {"pack_dir": str(Path(temp_dir) / "learnings")},
            }
            profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
            env = os.environ.copy()
            env["PITWALL_PROFILE"] = str(profile_path)
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pitwall",
                    "start",
                    "--no-watchdog",
                    "--record",
                    "off",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            def drain_output() -> None:
                assert proc is not None and proc.stdout is not None
                output.extend(proc.stdout)

            reader = threading.Thread(target=drain_output, daemon=True)
            reader.start()
            health_url = f"http://127.0.0.1:{http_port}/api/health"
            ready_deadline = time.monotonic() + 30
            while time.monotonic() < ready_deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"child exited early with code {proc.returncode}")
                payload = _health(health_url)
                if (
                    payload is not None
                    and "config_hash" in payload
                    and payload.get("config_error") is None
                    and "live" in payload
                ):
                    ready_elapsed = time.monotonic() - started
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("readiness timed out after 30 seconds")

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(
                    _packet(PacketId.SESSION, 1.0, 1),
                    ("127.0.0.1", udp_port),
                )
                sender.sendto(
                    _packet(PacketId.LAP_DATA, 1.1, 2),
                    ("127.0.0.1", udp_port),
                )

            telemetry_deadline = time.monotonic() + 10
            while time.monotonic() < telemetry_deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"child exited before telemetry became live with code {proc.returncode}"
                    )
                payload = _health(health_url)
                if (
                    payload is not None
                    and payload.get("live") is True
                    and payload.get("packet_age_ms") is not None
                ):
                    telemetry_elapsed = time.monotonic() - started
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("telemetry did not become live within 10 seconds")

            shutdown_started = time.monotonic()
            proc.send_signal(signal.SIGINT)
            try:
                returncode = proc.wait(timeout=20)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("child did not shut down within 20 seconds") from exc
            shutdown_elapsed = time.monotonic() - shutdown_started
            reader.join(timeout=2)
            if returncode != 0:
                raise RuntimeError(f"child exited with code {returncode} after SIGINT")
            captured = "".join(output)
            if "dashboard:" not in captured:
                raise RuntimeError('child output does not contain "dashboard:"')
            if "Traceback (most recent call last)" in captured:
                raise RuntimeError("child output contains a traceback")
        except Exception as exc:
            failure = str(exc)
        finally:
            if proc is not None:
                _stop_child(proc)
            if reader is not None:
                reader.join(timeout=2)

    if failure is not None:
        print(f"smoke: FAIL {failure}")
        captured = "".join(output).rstrip()
        print("smoke: child output:")
        print(captured or "(empty)")
        return 1

    print(
        "smoke: ok "
        f"(ready {ready_elapsed:.2f}s, telemetry {telemetry_elapsed:.2f}s, "
        f"shutdown {shutdown_elapsed:.2f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
