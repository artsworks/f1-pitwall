from __future__ import annotations

import argparse
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from pitwall import cli
from pitwall.store.db import Database, _uid_to_sql


def test_start_leaves_session_open_and_clears_heartbeat_on_clean_ctrl_c(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "pitwall.sqlite"
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        json.dumps(
            {
                "persistence": {"enabled": True, "path": str(db_path)},
                "recording": {"enabled": False, "directory": str(tmp_path / "recordings")},
                "speech": {"enabled": False},
                "learning": {"pack_dir": str(tmp_path / "learning")},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PITWALL_PROFILE", str(profile_path))
    uid = 123

    async def fake_serve(
        engine: Any,
        hub: Any,
        store: Any,
        live: Coroutine[Any, Any, None],
        **kwargs: Any,
    ) -> None:
        live.close()
        engine.db.upsert_session(uid, track_id=7, session_type=15, started_at=1.0)
        engine.state.session_uid = uid
        engine.db.write_heartbeat(uid, 1.0, 2.0, "", 1)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_serve", fake_serve)
    args = argparse.Namespace(child=False, no_watchdog=True, record=None)

    assert cli.cmd_start(args) == 0

    db = Database(db_path)
    session = db.session_row(uid)
    assert session is not None and session["ended_at"] is None
    assert db.read_heartbeat() is None
    assert db.maintenance_version(f"graded:{_uid_to_sql(uid)}") == 0
