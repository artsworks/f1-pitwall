from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pitwall.learnpack
from pitwall.audio.dispatcher import Call
from pitwall.audio.speaker import STARTUP_LINES, STARTUP_PRIORITY
from pitwall.cli import serve as cli
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


def _fake_serve_session_end(uid: int):  # type: ignore[no-untyped-def]
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
        engine.state.session_type = 15
        engine.state.track_id = 7
        engine.state.session_ended = True
        engine.tick(engine.clock.now())
        raise KeyboardInterrupt

    return fake_serve


def _start_args() -> argparse.Namespace:
    return argparse.Namespace(child=False, no_watchdog=True, record=None, log_level="warning")


def test_speak_startup_warms_before_speaking_at_current_time() -> None:
    events: list[tuple[str, object]] = []
    clock = [10.0]

    class Warmable:
        def warm(self, phrases: list[tuple[str, int]]) -> int:
            events.append(("warm", phrases))
            clock[0] = 20.0
            return len(phrases)

        def speak(self, call: Call) -> None:
            events.append(("speak", call))

    speaker = Warmable()
    thread = cli._speak_startup(speaker, STARTUP_LINES, lambda: clock[0], prerender=True)

    assert thread is not None
    assert thread.name == "startup-voice"
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert events[0] == (
        "warm",
        [(text, STARTUP_PRIORITY) for _, text in STARTUP_LINES],
    )
    calls = [event for name, event in events if name == "speak"]
    assert len(calls) == len(STARTUP_LINES)
    for (call_id, text), event in zip(STARTUP_LINES, calls, strict=True):
        assert isinstance(event, Call)
        assert (event.id, event.rule_id, event.priority, event.text) == (
            call_id,
            "startup",
            STARTUP_PRIORITY,
            text,
        )
        assert (event.deadline_ms, event.lap, event.tags, event.t, event.trigger_t) == (
            5000,
            0,
            [],
            20.0,
            20.0,
        )


def test_speak_startup_speaks_after_warm_failure(caplog) -> None:  # type: ignore[no-untyped-def]
    spoken: list[Call] = []

    class Warmable:
        def warm(self, phrases: list[tuple[str, int]]) -> int:
            raise RuntimeError("render failed")

        def speak(self, call: Call) -> None:
            spoken.append(call)

    thread = cli._speak_startup(Warmable(), STARTUP_LINES, lambda: 1.0, prerender=True)

    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert [call.text for call in spoken] == [text for _, text in STARTUP_LINES]
    assert "startup lines not pre-rendered: render failed" in caplog.text


def test_speak_startup_skips_warming_when_unavailable_or_disabled() -> None:
    class Direct:
        def __init__(self) -> None:
            self.calls: list[Call] = []

        def speak(self, call: Call) -> None:
            self.calls.append(call)

    class Warmable(Direct):
        def warm(self, phrases: list[tuple[str, int]]) -> int:
            raise AssertionError("warm should not run")

    direct = Direct()
    assert cli._speak_startup(direct, STARTUP_LINES, lambda: 1.0, prerender=True) is None
    assert [call.text for call in direct.calls] == [text for _, text in STARTUP_LINES]

    warmable = Warmable()
    assert cli._speak_startup(warmable, STARTUP_LINES, lambda: 1.0, prerender=False) is None
    assert [call.text for call in warmable.calls] == [text for _, text in STARTUP_LINES]


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


def test_start_runs_upkeep_and_pack_at_session_end(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = _make_profile(tmp_path, monkeypatch)
    uid = 654
    monkeypatch.setattr(cli, "_serve", _fake_serve_session_end(uid))

    import pitwall.maintenance

    real_maintain = pitwall.maintenance.maintain
    calls: list[tuple[bool | None, Any]] = []

    def tracked_maintain(*args: Any, **kwargs: Any) -> Any:
        report = real_maintain(*args, **kwargs)
        calls.append((kwargs.get("refit"), report))
        return report

    monkeypatch.setattr(pitwall.maintenance, "maintain", tracked_maintain)

    assert cli.cmd_start(_start_args()) == 0

    assert len(calls) == 2
    assert calls[1][0] is True
    lines = capsys.readouterr().out.splitlines()
    progress = "learning: session ended, updating track learning…"
    progress_index = lines.index(progress)
    second_report = calls[1][1]
    assert lines[progress_index + 1] == f"learning: {second_report.summary()}"
    assert lines[progress_index + 2].startswith("learning pack: saved ")

    session = Database(db_path).session_row(uid)
    assert session is not None and session["ended_at"] is not None
    assert Database(db_path).maintenance_version(f"graded:{_uid_to_sql(uid)}") == 1


def test_start_session_end_upkeep_failure_still_saves_pack(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    _make_profile(tmp_path, monkeypatch)
    uid = 987
    monkeypatch.setattr(cli, "_serve", _fake_serve_session_end(uid))

    import pitwall.maintenance

    real_maintain = pitwall.maintenance.maintain
    calls = 0

    def fail_session_end_maintain(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("boom")
        return real_maintain(*args, **kwargs)

    monkeypatch.setattr(pitwall.maintenance, "maintain", fail_session_end_maintain)

    assert cli.cmd_start(_start_args()) == 0
    lines = capsys.readouterr().out.splitlines()
    skipped_index = lines.index("learning: upkeep skipped (boom)")
    assert lines[skipped_index + 1].startswith("learning pack: saved ")


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


def test_start_supervised_forwards_log_level(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = SimpleNamespace(
        recording=SimpleNamespace(profile="off", directory=str(tmp_path), enabled=False),
        connection=SimpleNamespace(udp_host="127.0.0.1", udp_port=20777),
        engine="test",
    )
    store = SimpleNamespace(current=lambda: settings)
    captured: dict[str, Any] = {}

    class FakeSupervisor:
        def __init__(
            self,
            _engine: str,
            _recorder: Any,
            cmd: list[str],
            _directory: Path,
            **_kwargs: Any,
        ) -> None:
            captured["cmd"] = cmd

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(cli, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(cli, "runtime_dir", lambda _directory: tmp_path / "runtime")

    args = argparse.Namespace(record=None, log_level="debug")
    assert cli._start_supervised(args, store) == 0

    assert captured["cmd"][3:] == ["--log-level", "debug", "start", "--child"]
    assert captured["ran"]
