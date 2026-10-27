from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "PITWALL_HOME"


def storage_path(value: str) -> Path:
    """A storage setting as a path. Relative values resolve against PITWALL_HOME
    (set by the installed `pitwall` command) so they stay in the install directory."""
    path = Path(value).expanduser()
    home = os.environ.get(HOME_ENV)
    if home and not path.is_absolute():
        return Path(home).expanduser() / path
    return path
