#!/usr/bin/env python3
"""Run a bounded, read-only threshold search."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pitwall.trainloop import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
