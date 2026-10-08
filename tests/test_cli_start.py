from __future__ import annotations

import argparse
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pitwall.learnpack
from pitwall import cli
from pitwall.store.db import Database, _uid_to_sql


def _make_profile(tmp_path: Path, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
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
    return db_path


def _fake_serve(uid: int):  # type: ignore[no-untyped-def]
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

    return fake_serve


def _start_args() -> argparse.Namespace:
    return argparse.Namespace(child=False, no_watchdog=True, record=None)


def test_start_leaves_session_open_and_clears_heartbeat_on_clean_ctrl_c(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    db_path = _make_profile(tmp_path, monkeypatch)
    uid = 123

    monkeypatch.setattr(cli, "_serve", _fake_serve(uid))

    assert cli.cmd_start(_start_args()) == 0

    db = Database(db_path)
    session = db.session_row(uid)
    assert session is not None and session["ended_at"] is None
    assert db.read_heartbeat() is None
    assert db.maintenance_version(f"graded:{_uid_to_sql(uid)}") == 0


def test_start_shutdown_pack_refreshes_only_current_session(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _make_profile(tmp_path, monkeypatch)
    uid = 456

    monkeypatch.setattr(cli, "_serve", _fake_serve(uid))
    captured: dict[str, Any] = {}

    def fake_write_pack(db: Any, pack_dir: Path, **kwargs: Any) -> Path:
        captured.update(kwargs)
        return Path(pack_dir) / "pack.json"

    monkeypatch.setattr(pitwall.learnpack, "write_pack", fake_write_pack)

    assert cli.cmd_start(_start_args()) == 0
    assert captured["refresh_quality"] == [uid]


def test_start_second_ctrl_c_during_pack_write_exits_quietly(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    db_path = _make_profile(tmp_path, monkeypatch)
    uid = 789

    monkeypatch.setattr(cli, "_serve", _fake_serve(uid))

    def fake_write_pack(db: Any, pack_dir: Path, **kwargs: Any) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(pitwall.learnpack, "write_pack", fake_write_pack)

    assert cli.cmd_start(_start_args()) == 0
    assert "learning pack: interrupted" in capsys.readouterr().out
    assert Database(db_path).read_heartbeat() is None
