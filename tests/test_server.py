"""M1 part 2: WS protocol, hub broadcast, speaker, doctor."""

from __future__ import annotations

import base64
import io
import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from pitwall.audio.dispatcher import Call
from pitwall.audio.speaker import NullSpeaker
from pitwall.config.loader import ConfigStore
from pitwall.metrics import Metrics
from pitwall.server.app import create_app
from pitwall.server.hub import Hub
from pitwall.state.session import SessionState

_now = 0.0


def _call(call_id: str = "c1", priority: int = 2) -> Call:
    return Call(
        id=call_id,
        rule_id="r",
        priority=priority,
        text="Front left is cold.",
        tags=["tyres"],
        deadline_ms=3000,
        lap=2,
        t=_now,
        trigger_t=_now,
        still_true=lambda s: True,
    )


def _app(store: ConfigStore | None = None) -> tuple[TestClient, Hub]:
    hub = Hub()
    metrics = Metrics()
    state = SessionState()

    client = TestClient(
        create_app(
            hub,
            store or ConfigStore(),
            metrics,
            speaker_name="null",
            latest_snapshot=lambda: state.snapshot(_now),
        )
    )
    return client, hub


def test_websocket_hello_and_state_shape() -> None:
    client, hub = _app()
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["payload"]["protocol"] == 1
        assert hello["payload"]["config_hash"]
        assert hello["payload"]["audio"] is False
        ws.send_json({"type": "hello", "v": 1, "last_seq": 7})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        for key in ("live", "tyres", "brakes", "phase", "latency"):
            assert key in snap["payload"]
        for corner in ("fl", "fr", "rl", "rr"):
            assert set(snap["payload"]["tyres"][corner]) == {
                "surface",
                "inner",
                "wear",
                "status",
            }
        assert "calls" in snap["payload"]


def test_websocket_audio_subscription_is_removed_on_disconnect() -> None:
    client, hub = _app()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 1, "last_seq": None})
        ws.receive_json()
        ws.send_json({"type": "audio", "on": True})
        end = time.monotonic() + 2
        while not hub.audio_clients and time.monotonic() < end:
            time.sleep(0.01)
        assert len(hub.audio_clients) == 1
    assert not hub.audio_clients


def test_websocket_version_mismatch_close() -> None:
    client, _ = _app()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 99, "last_seq": None})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_hub_broadcast_order_and_snapshot() -> None:
    hub = Hub()
    hub.speak(_call("a"))
    hub.spoken("a", _now)
    hub.cancel("a")
    types = [f["type"] for f in hub.outbox]
    assert types == ["call", "spoken", "cancel"]
    assert len(hub.recent_calls) == 1
    # reconnect snapshot carries last calls
    client, _ = _app()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 1, "last_seq": 0})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"


def test_hub_audio_is_not_buffered_without_subscribers() -> None:
    hub = Hub()
    hub.audio("c1", 2, b"wav")
    assert hub.outbox == []


def test_hub_audio_frame_targets_audio_clients_without_loop() -> None:
    hub = Hub()
    client = object()
    wav = b"test wav"
    hub.audio_clients.add(client)
    hub.audio("c1", 1, wav)
    assert len(hub.outbox) == 1
    frame = hub.outbox[0]
    assert frame["type"] == "audio"
    assert frame["payload"]["id"] == "c1"
    assert frame["payload"]["priority"] == 1
    assert frame["payload"]["format"] == "wav"
    assert base64.b64decode(frame["payload"]["data"]) == wav
    assert len(hub.recent_calls) == 0


def test_reconnect_snapshot_has_calls() -> None:
    hub = Hub()
    metrics = Metrics()
    store = ConfigStore()
    hub.speak(_call("x"))
    client = TestClient(create_app(hub, store, metrics))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "v": 1, "last_seq": 3})
        snap = ws.receive_json()
        calls = snap["payload"]["calls"]
        assert any(c["id"] == "x" for c in calls)


def test_api_health_and_config() -> None:
    client, _ = _app()
    h = client.get("/api/health").json()
    assert h["speaker"] == "null"
    assert "config_hash" in h
    c = client.get("/api/config").json()
    assert c["ui"]["state_hz"] == 5
    assert client.get("/").status_code == 200
    assert client.get("/radio").status_code == 200


def test_null_speaker_on_spoken() -> None:
    speaker = NullSpeaker()
    got: list[tuple[str, float]] = []
    speaker.on_spoken = lambda cid, t: got.append((cid, t))
    speaker.speak(_call("n"))
    assert got and got[0][0] == "n"
    speaker.cancel("n")  # no-op, must not raise


def test_sapi_not_importable_on_linux() -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("windows-only check")
    assert "win32com" not in sys.modules
    assert "pythoncom" not in sys.modules


def test_doctor_pass_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from pitwall import doctor

    out = io.StringIO()

    async def fake_listen(host: str, port: int, ingest: object, clock: object) -> object:
        class T:
            def get_extra_info(self, name: str) -> None:
                return None

            def close(self) -> None:
                pass

        return T()

    monkeypatch.setattr(doctor, "listen", fake_listen)

    async def _noop(s: float) -> None:
        pass

    monkeypatch.setattr(doctor.asyncio, "sleep", _noop)

    rc = doctor.run_doctor(seconds=0, out=out, store=ConfigStore())
    text = out.getvalue()
    assert "PASS" in text
    assert "config loads" in text
    assert "UDP receive buffer 0.0 MiB" in text
    assert rc in (0, 1)  # FAIL only if env is bad; lines must render


def test_cli_start_help(capsys: pytest.CaptureFixture[str]) -> None:
    from pitwall.cli import main

    with pytest.raises(SystemExit) as e:
        main(["start", "--help"])
    assert e.value.code == 0
    assert "usage" in capsys.readouterr().out


class _FakeVoice:
    def __init__(self, done_after: int = 1) -> None:
        self.calls: list[tuple[str, int]] = []
        self._polls = 0
        self._done_after = done_after

    def Speak(self, text: str, flags: int) -> int:
        self.calls.append((text, flags))
        return 0

    def WaitUntilDone(self, ms: int) -> bool:
        self._polls += 1
        return self._polls >= self._done_after


def _urgent_event():
    import threading

    return threading.Event()


def test_speak_one_p1_purge_and_on_spoken() -> None:
    from pitwall.audio.speaker import SVSF_ASYNC, SVSF_PURGE_BEFORE_SPEAK, _speak_one

    voice = _FakeVoice()
    got: list[str] = []
    urgent = _urgent_event()
    urgent.set()
    done = _speak_one(
        voice, _call("p1", priority=1), urgent, lambda cid, t: got.append(cid), blip=None
    )
    assert voice.calls[0][1] == SVSF_ASYNC | SVSF_PURGE_BEFORE_SPEAK
    assert got == ["p1"]
    assert done is True
    assert not urgent.is_set()  # cleared after the P1 call


def test_speak_one_p2_async_only() -> None:
    from pitwall.audio.speaker import SVSF_ASYNC, _speak_one
    from pitwall.audio.speaker import SVSF_PURGE_BEFORE_SPEAK as PURGE

    voice = _FakeVoice()
    done = _speak_one(voice, _call("p2", priority=2), _urgent_event(), None, blip=None)
    assert voice.calls[0][1] == SVSF_ASYNC
    assert voice.calls[0][1] & PURGE == 0
    assert done is True


def test_speak_one_wait_breaks_on_urgent() -> None:
    from pitwall.audio.speaker import _speak_one

    voice = _FakeVoice(done_after=100)
    urgent = _urgent_event()
    urgent.set()
    done = _speak_one(voice, _call("p2", priority=2), urgent, None, blip=None)
    assert done is False


def test_packet_age_uses_receive_clock_not_session_time() -> None:
    """Session time (seconds since session start) must never be mixed with the
    tick clock; a packet received 0.1 s ago is LIVE whatever its session_time."""
    from pitwall.protocol.header import PacketId, parse_header
    from pitwall.server.app import state_payload

    from .synth import make_packet

    store = ConfigStore()
    store.poll(0.0)
    settings = store.current()
    state = SessionState()
    payload = make_packet(PacketId.CAR_TELEMETRY, session_time=4879.5)
    recv = 1_000_000.0
    state.on_packet(parse_header(payload), payload, recv)
    snap = state.snapshot(recv + 0.1)
    assert snap.last_packet_t == recv
    body = state_payload(snap, settings=settings, metrics=Metrics(), quiet=False)
    assert body["live"] is True
    assert 90.0 <= body["packet_age_ms"] <= 110.0
    later = state_payload(
        state.snapshot(recv + 5), settings=settings, metrics=Metrics(), quiet=False
    )
    assert later["live"] is False


def test_health_includes_packet_age_and_live() -> None:
    from pitwall.server.app import packet_age_ms

    hub = Hub()
    metrics = Metrics()
    state = SessionState()
    snap = state.snapshot(0.0)
    hub.health_source = lambda: {
        "packet_age_ms": packet_age_ms(snap),
        "live": False,
    }
    client = TestClient(create_app(hub, ConfigStore(), metrics))
    h = client.get("/api/health").json()
    assert "packet_age_ms" in h
    assert "live" in h
    assert h["live"] is False


def test_packet_age_ms_helper() -> None:
    from pitwall.server.app import packet_age_ms

    snap = SessionState().snapshot(2.0)
    snap.last_packet_t = 1.5
    assert packet_age_ms(snap) == 500.0
    snap.last_packet_t = None
    assert packet_age_ms(snap) is None


def test_replay_hub_sink_emits_call(tmp_path) -> None:
    """build_engine with a Hub sink: a firing rule produces a call frame."""
    import asyncio

    from pitwall.audio.dispatcher import LogSink
    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine, run_replay
    from pitwall.net.recording import RecordingWriter
    from pitwall.protocol.header import PacketId

    # recording: race session + lap data on_track + car damage FL wing 30
    from .synth import pack_packet

    packets = (
        [pack_packet(PacketId.SESSION, {"session_type": 15}, session_time=t) for t in (0.0, 1.0)]
        + [
            pack_packet(
                PacketId.LAP_DATA,
                {"cars": {0: {"driver_status": 4, "current_lap_num": 5}}},
                session_time=t,
            )
            for t in (0.0, 1.0)
        ]
        + [
            pack_packet(
                PacketId.CAR_DAMAGE,
                {"cars": {0: {"front_left_wing_damage": 30}}},
                session_time=t,
            )
            for t in (0.5, 1.5)
        ]
    )
    path = tmp_path / "d.f1bin"
    with RecordingWriter(path, packet_format=2026) as w:
        for i, pkt in enumerate(packets):
            w.write_datagram(i * 0.5, pkt)

    hub = Hub()
    engine = build_engine(clock=VirtualClock(), sinks=[hub, LogSink()])
    asyncio.run(run_replay(path, engine, None))
    call_frames = [f for f in hub.outbox if f["type"] == "call"]
    assert call_frames
    assert any("wing" in f["payload"]["text"].lower() for f in call_frames)


def test_state_payload_stale_after_gap() -> None:
    """Snapshot built long after the last packet is not live."""
    from pitwall.server.app import state_payload

    snap = SessionState().snapshot(5.0)
    payload = state_payload(snap, settings=ConfigStore().current(), metrics=Metrics(), quiet=False)
    assert payload["live"] is False
    assert payload["packet_age_ms"] is None

    # feed a packet, then snapshot 5 s later -> stale
    from pitwall.ingest import Ingest
    from pitwall.protocol.header import PacketId

    from .synth import pack_packet

    state2 = SessionState()
    ingest2 = Ingest()
    state2.register(ingest2)
    ingest2.on_datagram(pack_packet(PacketId.LAP_DATA), 0.0)
    snap2 = state2.snapshot(5.0)
    payload2 = state_payload(
        snap2, settings=ConfigStore().current(), metrics=Metrics(), quiet=False
    )
    assert payload2["live"] is False
    assert payload2["packet_age_ms"] == 5000.0


def test_hub_recent_calls_track_audio_outcome() -> None:
    hub = Hub()
    for cid in ("a", "b", "c"):
        hub.speak(_call(cid))
    hub.spoken("a", _now)
    hub.cancel("a")
    hub.spoken("b", _now)
    hub.cancel("c")
    by_id = {c["id"]: c for c in hub.recent_calls}
    assert by_id["a"]["audio"] == "interrupted"
    assert by_id["b"]["audio"] == "started"
    assert by_id["c"]["audio"] == "dropped"
    assert all(isinstance(c["t"], float) for c in hub.recent_calls)


def test_state_payload_quali_zone() -> None:
    import math

    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics
    from pitwall.server.app import state_payload
    from pitwall.state.session import Snapshot

    settings = ConfigStore().current()
    race = state_payload(
        Snapshot(now=1.0, session_kind="race"), settings=settings, metrics=Metrics(), quiet=False
    )
    assert race["quali"] is None
    garage = state_payload(
        Snapshot(
            now=1.0,
            session_kind="qualifying",
            phase="garage",
            release_clean=False,
            release_wait_s=7.0,
            release_gap_ahead_s=math.inf,
            cars_on_track=3,
            red_flag=True,
        ),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
        quiet_left_s=120.0,
    )
    assert garage["red_flag"] is True
    assert garage["quiet"] is True and garage["quiet_left_s"] == 120.0
    rel = garage["quali"]["release"]
    assert rel == {
        "clean": False,
        "wait_s": 7.0,
        "gap_ahead_s": None,
        "gap_behind_s": None,
        "cars_on_track": 3,
        "time_for_out_lap": True,
    }
    flying = state_payload(
        Snapshot(
            now=1.0,
            session_kind="qualifying",
            phase="flying",
            projected_lap_ms=90_700,
            quali_cutoff_ms=90_000,
            abort_advised=True,
        ),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    assert flying["quali"]["lap"] == {"projected_ms": 90_700, "delta_ms": 700, "abort": True}
    assert "release" not in flying["quali"]


def test_state_payload_quali_pole_available_outside_cool_lap() -> None:
    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics
    from pitwall.server.app import state_payload
    from pitwall.state.session import Snapshot

    settings = ConfigStore().current()
    payload = state_payload(
        Snapshot(
            now=1.0,
            session_kind="qualifying",
            phase="flying",
            pole_gap_ms=420,
            pole_driver="NORRIS",
            pole_sector_gaps_ms=(100, 350, -30),
            best_sectors_ms=(30_100, 40_350, 20_970),
            pole_sectors_ms=(30_000, 40_000, 21_000),
            pole_worst_sector=2,
        ),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    quali = payload["quali"]
    assert quali["pole"] == {
        "driver": "NORRIS",
        "gap_ms": 420,
        "sector_gaps_ms": [100, 350, -30],
        "sectors_ms": [30_100, 40_350, 20_970],
        "pole_sectors_ms": [30_000, 40_000, 21_000],
        "worst_sector": 2,
    }
    assert "cool" not in quali

    without_pole = state_payload(
        Snapshot(now=1.0, session_kind="qualifying", pole_gap_ms=0),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    assert without_pole["quali"]["pole"] is None


def test_state_payload_quali_teammate_benchmark() -> None:
    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics
    from pitwall.server.app import state_payload
    from pitwall.state.session import Snapshot

    settings = ConfigStore().current()
    payload = state_payload(
        Snapshot(
            now=1.0,
            session_kind="qualifying",
            phase="flying",
            pole_gap_ms=420,
            pole_driver="NORRIS",
            best_sectors_ms=(30_100, 40_350, 20_970),
            teammate_driver="PIASTRI",
            teammate_gap_ms=-150,
            teammate_sector_gaps_ms=(-200, 80, -30),
            teammate_sectors_ms=(30_300, 40_270, 21_000),
            teammate_worst_sector=2,
        ),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    quali = payload["quali"]
    assert quali["teammate"] == {
        "driver": "PIASTRI",
        "gap_ms": -150,
        "sector_gaps_ms": [-200, 80, -30],
        "sectors_ms": [30_100, 40_350, 20_970],
        "teammate_sectors_ms": [30_300, 40_270, 21_000],
        "worst_sector": 2,
    }
    assert quali["pole"]["driver"] == "NORRIS"

    without_mate = state_payload(
        Snapshot(now=1.0, session_kind="qualifying", pole_gap_ms=420),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    assert without_mate["quali"]["teammate"] is None
    assert without_mate["quali"]["pole"] is not None


def test_pit_board_payload() -> None:
    from pitwall.protocol.layouts import Corners
    from pitwall.server.app import pit_board_payload
    from pitwall.state.pressure import PressureCall
    from pitwall.state.session import Snapshot

    assert pit_board_payload(Snapshot(now=1.0, phase="flying")) is None
    calls = (
        PressureCall("fl", "front left", 85.0, "small", 0.0, 0.0, True, -0.2),
        PressureCall("rr", "rear right", 107.0, "medium", 0.4, 21.4),
        PressureCall("rl", "rear left", 107.0, "medium", 0.4, 21.4),
    )
    board = pit_board_payload(
        Snapshot(
            now=1.0,
            phase="garage",
            run_flying_s=90.0,
            pressure_advice=calls,
            pressure_advice_text="rears up 0.4",
            setup_tyre_pressure=Corners(rl=21.0, rr=21.4, fl=22.5, fr=23.0),
            setup={"front_wing": 12},
            fuel_remaining_laps=1.6,
        ),
        {"fuel_push_need_laps": 0.9},
    )
    assert board is not None
    t = board["tyres"]
    assert t["fl"]["limited"] and t["fl"]["edge"] == "min" and t["fl"]["target_psi"] is None
    assert t["rl"]["target_psi"] == 21.4 and not t["rl"]["applied"]
    assert t["rr"]["applied"]  # already dialled in
    assert t["fr"] == {
        "psi": 23.0,
        "target_psi": None,
        "delta_psi": 0.0,
        "limited": False,
        "edge": None,
        "avg_c": None,
        "applied": False,
    }
    assert board["setup"] == {"front_wing": 12}
    assert board["fuel_need_laps"] == 0.9 and board["has_advice"]


def test_strategy_payload_race_contract() -> None:
    """Zone F contract: pit window, immediate neighbours only, stint plan,
    backend-owned fuel delta; absent outside races."""
    import dataclasses

    from pitwall.server.app import state_payload, strategy_payload
    from pitwall.state.session import Snapshot

    store = ConfigStore()
    store.poll(0.0)
    settings = store.current()
    assert strategy_payload(Snapshot(now=0.0)) is None
    snap = dataclasses.replace(
        Snapshot(now=0.0),
        session_type=15,
        session_kind="race",
        race_phase="racing",
        lap_num=20,
        laps_remaining=38,
        tyre_visual=17,
        pit_window_start=26,
        pit_window_end=28,
        pit_plan="undercut",
        pit_plan_lap=26,
        rival_ahead_idx=3,
        rival_ahead_pos=4,
        rival_ahead_name="Norris",
        rival_ahead_compound=18,
        rival_ahead_pace_ms=91_200,
        predicted_lap_ms=91_000,
        gap_ahead_s=1.4,
        gap_trend_ahead_s=0.3,
        rival_behind_idx=5,
        rival_behind_pos=6,
        rival_behind_name="Leclerc",
        gap_behind_s=0.8,
        undercut_s=1.2,
        fuel_margin_laps=0.4,
        fuel_source="fit",
    )
    s = strategy_payload(snap, settings.thresholds)
    assert s is not None
    assert s["pit_window"] == {"start": 26, "end": 28}
    assert s["ahead"]["pos"] == 4 and s["ahead"]["compound"] == "HARD"
    assert s["ahead"]["pace_delta_s"] == 0.2 and s["ahead"]["gap_trend_s"] == 0.3
    assert s["behind"]["overtake"] is True and s["ahead"]["overtake"] is False
    assert s["undercut_s"] == 1.2 and s["fuel_delta_laps"] == 0.4
    assert "L26–28" in s["stint_plan"]
    body = state_payload(snap, settings=settings, metrics=Metrics(), quiet=False, page="car")
    assert body["strategy"]["ahead"]["name"] == "Norris"
    assert body["page"] == "car" and "car" in body["pages"]
    assert body["track_info"]["gap_ahead_s"] == 1.4
    assert body["setup"] is None


def test_strategy_payload_overtake_2026() -> None:
    import dataclasses

    from pitwall.server.app import strategy_payload
    from pitwall.state.session import Snapshot

    snap = dataclasses.replace(
        Snapshot(now=0.0),
        session_type=15,
        session_kind="race",
        race_phase="racing",
        regulations_2026=True,
        rival_ahead_idx=3,
        gap_ahead_s=0.4,
        rival_ahead_overtake=False,
        rival_behind_idx=5,
        gap_behind_s=2.5,
        rival_behind_overtake=True,
        overtake_available=1,
        overtake_active=0,
        active_aero_mode=1,
    )
    s = strategy_payload(snap, {"drs_detection_gap_s": 1.0})
    assert s is not None
    assert s["ahead"]["overtake"] is False and s["behind"]["overtake"] is True
    assert s["overtake"] is False and s["overtake_earned"] is True and s["aero_mode"] == 1
    active = strategy_payload(dataclasses.replace(snap, overtake_active=1))
    assert active is not None and active["overtake"] is True and active["overtake_earned"] is False
    sc = strategy_payload(dataclasses.replace(snap, safety_car_status=1))
    assert sc is not None and sc["behind"]["overtake"] is False


def test_strategy_payload_named_plans() -> None:
    """Named plans A/B/C, active plan and on-plan state (docs/15 zone F)."""
    import dataclasses

    from pitwall.server.app import strategy_payload
    from pitwall.state.session import Snapshot
    from pitwall.strategy.plans import StrategyPlan

    base = dataclasses.replace(
        Snapshot(now=0.0),
        session_type=15,
        session_kind="race",
        race_phase="racing",
        lap_num=20,
        laps_remaining=38,
        tyre_visual=17,
    )
    s = strategy_payload(base)
    assert s is not None
    assert s["plans"] == [] and s["active_plan"] is None and s["on_plan"] is None
    assert s["plan_switch"] is None
    plans = (
        StrategyPlan("A", "primary", (17, 18), (27,), (26, 28), 5000.0, 0.0),
        StrategyPlan("B", "alternative", (17, 18, 16), (18, 40), (17, 19), 5004.2, 4.2),
        StrategyPlan("C", "reactive", (17, 18), (20,), (20, 20), 4990.0, -10.0),
    )
    snap = dataclasses.replace(
        base,
        plans=plans,
        active_plan="B",
        on_plan=False,
        plan_off_s=4.2,
        plan_target_lap=18,
        plan_switch_count=1,
        plan_switched_from="A",
        plan_switch_reason="pace",
        plan_switch_lap=15,
    )
    s = strategy_payload(snap)
    assert s is not None
    assert [p["id"] for p in s["plans"]] == ["A", "B", "C"]
    a, b, c = s["plans"]
    assert a["label"] == "1-stop M-H" and a["stops"] == 1 and a["window"] == [26, 28]
    assert a["compounds"] == ["MEDIUM", "HARD"] and a["stop_laps"] == [27]
    assert b["active"] and not a["active"] and b["delta_s"] == 4.2 and b["stops"] == 2
    assert c["kind"] == "reactive" and c["delta_s"] == -10.0
    assert s["active_plan"] == "B" and s["on_plan"] is False and s["plan_off_s"] == 4.2
    assert s["plan_target_lap"] == 18
    assert s["plan_switch"] == {"from": "A", "reason": "pace", "lap": 15}


def test_state_payload_session_label_and_weekend() -> None:
    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics
    from pitwall.server.app import state_payload
    from pitwall.state.session import Snapshot

    settings = ConfigStore().current()
    structure = (1, 10, 11, 12, 15, 5, 6, 7, 16)
    sprint = state_payload(
        Snapshot(now=1.0, session_kind="race", session_type=15, weekend_structure=structure),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    assert sprint["session_label"] == "Sprint"
    assert sprint["weekend"] == ["FP1", "SQ1", "SQ2", "SQ3", "Sprint", "Q1", "Q2", "Q3", "Race"]
    race = state_payload(
        Snapshot(now=1.0, session_kind="race", session_type=16, weekend_structure=structure),
        settings=settings,
        metrics=Metrics(),
        quiet=False,
    )
    assert race["session_label"] == "Race"
