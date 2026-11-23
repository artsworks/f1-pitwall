from __future__ import annotations

import subprocess
import sys

_QUERY = [
    "nvidia-smi",
    "--query-gpu=utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
]


def gpu_status() -> str | None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            _QUERY, capture_output=True, text=True, timeout=2.0, creationflags=flags
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    try:
        utilization, used, total = (int(part) for part in lines[0].split(","))
    except ValueError:
        return None
    return f"gpu {utilization} %, {used}/{total} MB"
