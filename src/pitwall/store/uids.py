"""Session UIDs for derived synthetic sessions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

SYNTHETIC_UID_TAG = 0xF1DE57
_TAG_SHIFT = 40


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
