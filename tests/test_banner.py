from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import pitwall.cli.serve as cli
from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream


def test_banner_groups_and_aligns_entries() -> None:
    rendered = cli._banner(
        [
            ("learning", "upkeep complete"),
            ("dashboard", "http://localhost:8000"),
            ("LAN", "http://192.168.1.2:8000"),
            ("PIN", "1234  (LAN devices only)"),
            ("telemetry", "UDP 127.0.0.1:20777"),
            ("recording", "off"),
            ("speech", "null"),
            ("pitwall", "12 track minutes"),
            ("watchdog", None),
        ]
    )
    groups = rendered.split("\n\n")
    assert len(groups) == 3
    assert groups[0].startswith("  dashboard")
    assert "  LAN" in groups[0]
    assert "PIN" in groups[0]
    assert "telemetry" in groups[1] and "recording" in groups[1] and "speech" in groups[1]
    assert "learning" in groups[2] and "pitwall" in groups[2]
    assert "watchdog" not in rendered
    first_url = groups[0].splitlines()[:2]
    assert first_url[0].index("http://") == first_url[1].index("http://")
    assert not rendered.endswith("\n")


def test_banner_parses_scorecard_and_preserves_unlabeled_lines() -> None:
    rendered = cli._banner(
        [
            ("learning", "upkeep complete"),
            ("", "pitwall: 12 track minutes over 1 session"),
            ("", "unclassified scorecard detail"),
        ]
    )
    groups = rendered.split("\n\n")
    assert groups[0] == "  unclassified scorecard detail"
    lines = groups[1].splitlines()
    assert lines[0].index("upkeep") == lines[1].index("12")


def test_pin_box_uses_equal_width_ascii_lines_for_cp1252(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli.sys,
        "stdout",
        SimpleNamespace(encoding="cp1252", isatty=lambda: False),
    )
    lines = cli._pin_box("1234", "(LAN devices only)").splitlines()
    assert len({len(line) for line in lines}) == 1
    assert all(char in "\n".join(lines) for char in "+-|")
    assert "PIN" in "\n".join(lines)
    assert "1234" in "\n".join(lines)
    assert "(LAN devices only)" in "\n".join(lines)


def test_state_broadcast_logs_status_and_prints_pin_once_after_first_packet(
    monkeypatch, capsys, caplog
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.server.app as server_app

    monkeypatch.setattr(server_app, "state_payload", lambda *_args, **_kwargs: {})
    caplog.set_level(logging.DEBUG, logger="pitwall.cli.serve")

    class StopBroadcast(Exception):
        pass

    class FakeClock:
        sleeps = 0
        current = 0.0

        def now(self) -> float:
            return self.current

        async def sleep(self, _period: float) -> None:
            self.sleeps += 1
            if self.sleeps == 1:
                state.last_packet_t = 1.0
                self.current = 10.0
            elif self.sleeps == 2:
                raise StopBroadcast

    clock = FakeClock()
    state = SimpleNamespace(last_packet_t=None)
    engine = SimpleNamespace(
        last_snapshot=None,
        clock=clock,
        ingest=SimpleNamespace(
            census=lambda **_kwargs: {
                "raw_datagrams": 0,
                "dropped_unsupported": 0,
                "dropped_malformed": 0,
                "packets": {},
            }
        ),
        state=SimpleNamespace(
            snapshot=lambda _now: SimpleNamespace(last_packet_t=state.last_packet_t)
        ),
        metrics=SimpleNamespace(note_packet_to_ws=lambda *_args: None),
        dispatcher=SimpleNamespace(silent=False, quiet_until=None),
        mindset=None,
        page=None,
        menu_payload=lambda _now: {},
        voice_payload=lambda _now: {"available": False, "listening": False},
    )
    store = SimpleNamespace(
        current=lambda: SimpleNamespace(
            ui=SimpleNamespace(state_hz=1),
            policy=SimpleNamespace(quiet=False),
        )
    )
    hub = SimpleNamespace(broadcast=lambda *_args: None)

    with pytest.raises(StopBroadcast):
        asyncio.run(
            cli._state_broadcast(
                engine,
                hub,
                store,
                lambda: engine,
                pin="1234",
                pin_note="(LAN devices only)",
            )
        )

    output = capsys.readouterr().out
    assert output.count("telemetry connected") == 1
    assert output.count("1234") == 1
    assert "udp: " not in output
    status_records = [
        record
        for record in caplog.records
        if record.name == "pitwall.cli.serve" and record.getMessage().startswith("udp: ")
    ]
    assert len(status_records) == 1
    assert status_records[0].levelno == logging.DEBUG


class _StopBroadcast(Exception):
    pass


def _broadcast_frames(engine, frames: list, *, count: int = 1) -> list:
    """Run _state_broadcast until `count` frames are sent, then stop."""
    sent = frames

    async def stop_sleep(_period: float) -> None:
        if len(sent) >= count:
            raise _StopBroadcast

    engine.clock.sleep = stop_sleep
    hub = SimpleNamespace(broadcast=lambda kind, payload: sent.append((kind, payload)))
    with pytest.raises(_StopBroadcast):
        asyncio.run(cli._state_broadcast(engine, hub, engine.store, lambda: engine))
    return sent


def test_state_broadcast_uses_processed_snapshot(tmp_path: Path) -> None:
    # Rival 0.8 s ahead through the race -> Engine._battle marks it attacking.
    path = write_packet_stream(tmp_path / "r.f1bin", race_stream(RaceSpec(laps=6, gap_ahead_s=0.8)))
    engine = build_engine(clock=VirtualClock(), sinks=[])
    asyncio.run(run_replay(path, engine, None))
    assert engine.last_snapshot is not None

    # Prove the setup advisor graft lands on the snapshot the frames use.
    fields = {
        "setup_call_param": "brake_bias",
        "setup_call_from": 56.0,
        "setup_call_to": 57.0,
        "setup_call_reason": "test",
        "setup_stop_wing_from": 0.0,
        "setup_stop_wing_to": 0.0,
    }
    engine.setup_advisor.snapshot_fields = lambda: dict(fields)
    engine.tick(engine.clock.now())
    assert engine.last_snapshot.setup_call_param == "brake_bias"
    assert engine.last_snapshot.setup_call_to == 57.0

    (kind, payload), *_ = _broadcast_frames(engine, [])
    assert kind == "state"
    mode = payload["strategy"]["battle"]["mode"]
    assert mode == engine.last_snapshot.battle_mode
    assert mode != "free_air"
    # The same processed snapshot feeds the frame: its grafted fields match too.
    assert engine.last_snapshot.setup_call_param == "brake_bias"


def test_state_broadcast_falls_back_without_last_snapshot(tmp_path: Path) -> None:
    path = write_packet_stream(tmp_path / "r.f1bin", race_stream(RaceSpec(laps=3)))
    engine = build_engine(clock=VirtualClock(), sinks=[])
    asyncio.run(run_replay(path, engine, None))
    engine.last_snapshot = None
    (kind, _payload), *_ = _broadcast_frames(engine, [])
    assert kind == "state"
