from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pitwall.cli.serve as cli


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


def test_state_broadcast_prints_pin_once_after_first_packet(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import pitwall.server.app as server_app

    monkeypatch.setattr(server_app, "state_payload", lambda *_args, **_kwargs: {})

    class StopBroadcast(Exception):
        pass

    class FakeClock:
        sleeps = 0

        def now(self) -> float:
            return 0.0

        async def sleep(self, _period: float) -> None:
            self.sleeps += 1
            if self.sleeps == 1:
                state.last_packet_t = 1.0
            elif self.sleeps == 3:
                raise StopBroadcast

    clock = FakeClock()
    state = SimpleNamespace(last_packet_t=None)
    engine = SimpleNamespace(
        clock=clock,
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
