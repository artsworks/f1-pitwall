from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pytest

from pitwall.audio.dispatcher import Call, Dispatcher
from pitwall.audio.speaker import (
    ENGINES,
    EngineSpec,
    NullSpeaker,
    register_engine,
    streams_audio,
    wire_speaker,
)
from pitwall.audio.wav_speaker import to_wav
from pitwall.clock import VirtualClock
from pitwall.config.models import PolicySettings, SpeechSettings
from pitwall.metrics import Metrics
from pitwall.server.app import state_payload
from pitwall.server.hub import Hub
from pitwall.state.session import SessionState
from tests.audio_contract import make_call


class FakeSpeaker:
    name = "fake"
    speaks_audio = True
    supports_audio_stream = False
    on_spoken = None
    on_dropped = None

    def __init__(self) -> None:
        self.closed = False

    def speak(self, call: Call) -> None:
        pass

    def cancel(self, call_id: str) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeStreamSpeaker(FakeSpeaker):
    name = "fake-stream"
    supports_audio_stream = True
    on_audio = None


def _dispatcher(sinks: list[object] | None = None) -> Dispatcher:
    return Dispatcher(PolicySettings(), VirtualClock(), sinks=sinks)  # type: ignore[arg-type]


def test_registry_registers_and_creates_third_party_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    speaker = FakeSpeaker()
    spec = EngineSpec("contract-fake", lambda _settings: speaker, None)
    monkeypatch.setitem(ENGINES, spec.id, spec)
    register_engine(spec)

    assert (
        __import__("pitwall.audio.speaker", fromlist=["make_speaker"]).make_speaker(
            SpeechSettings(engine=spec.id)
        )
        is speaker
    )


def test_registry_walks_factory_fallback_and_unknown_ids(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = FakeSpeaker()
    good_spec = EngineSpec("contract-good", lambda _settings: good, None)
    broken_spec = EngineSpec(
        "contract-broken",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("factory failed")),
        lambda: good_spec.id,
    )
    monkeypatch.setitem(ENGINES, good_spec.id, good_spec)
    monkeypatch.setitem(ENGINES, broken_spec.id, broken_spec)
    register_engine(good_spec)
    register_engine(broken_spec)
    from pitwall.audio.speaker import make_speaker

    with caplog.at_level(logging.WARNING):
        assert make_speaker(SpeechSettings(engine=broken_spec.id)) is good
        monkeypatch.setattr("pitwall.audio.speaker.auto_engine", lambda _settings: good_spec.id)
        assert make_speaker(SpeechSettings(engine="missing-engine")) is good
    assert "factory failed" in caplog.text
    assert "missing-engine" in caplog.text


def test_registry_disabled_speech_uses_null_speaker() -> None:
    from pitwall.audio.speaker import make_speaker

    speaker = make_speaker(SpeechSettings(enabled=False, engine="contract-fake"))
    assert isinstance(speaker, NullSpeaker)


def test_stream_capability_requires_callback(caplog: pytest.LogCaptureFixture) -> None:
    class Noncompliant(FakeSpeaker):
        supports_audio_stream = True

    class SapiLike(FakeSpeaker):
        supports_audio_stream = False
        on_audio = None

    with caplog.at_level(logging.WARNING):
        assert not streams_audio(Noncompliant())
    assert "has no on_audio callback" in caplog.text
    assert not streams_audio(SapiLike())


def test_wire_speaker_updates_hub_audio_metrics_and_drop_state() -> None:
    speaker = FakeStreamSpeaker()
    hub = Hub()
    hub.audio_clients.add(object())
    dispatcher = _dispatcher([speaker])
    wire_speaker(speaker, hub, dispatcher)
    assert hub.streams_audio

    call = make_call("wire-started")
    hub.speak(call)
    handoff = time.time()
    dispatcher.metrics.note_handoff(call.id, handoff)
    assert speaker.on_audio is not None
    assert speaker.on_spoken is not None
    speaker.on_audio(call.id, call.priority, _small_wav())
    speaker.on_spoken(call.id, handoff + 0.125)
    assert hub.outbox[-2]["type"] == "audio"
    assert base64.b64decode(hub.outbox[-2]["payload"]["data"]) == _small_wav()
    assert {row["id"]: row["audio"] for row in hub.recent_calls}[call.id] == "started"
    assert dispatcher.metrics.summary()["tts_ms"]["p99"] == pytest.approx(125.0)

    dropped = make_call("wire-dropped")
    hub.speak(dropped)
    assert speaker.on_dropped is not None
    speaker.on_dropped(dropped.id)
    assert {row["id"]: row["audio"] for row in hub.recent_calls}[dropped.id] == "dropped"
    assert list(dispatcher._dropped) == [dropped.id]


def _small_wav() -> bytes:
    return to_wav(np.zeros(240, dtype=np.float32), 24_000)


def test_hub_audio_outcomes_follow_lifecycle() -> None:
    hub = Hub()
    hub.speak(make_call("started"))
    hub.speak(make_call("dropped"))
    assert {row["id"]: row["audio"] for row in hub.recent_calls} == {
        "started": "dispatched",
        "dropped": "dispatched",
    }

    before_audio = [row["audio"] for row in hub.recent_calls]
    hub.audio("started", 2, _small_wav())
    assert [row["audio"] for row in hub.recent_calls] == before_audio

    hub.spoken("started", time.time())
    hub.cancel("started")
    hub.cancel("dropped")
    hub.spoken("unknown", time.time())
    by_id = {row["id"]: row["audio"] for row in hub.recent_calls}
    assert by_id == {"started": "interrupted", "dropped": "dropped"}
    assert "unknown" not in by_id


def test_live_start_wires_registered_stream_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pitwall.cli import serve as cli
    from tests.test_cli_start import _make_profile, _start_args

    engine_id = "contract-start"
    speaker = FakeStreamSpeaker()
    spec = EngineSpec(engine_id, lambda _settings: speaker, None)
    monkeypatch.setitem(ENGINES, engine_id, spec)
    register_engine(spec)
    _make_profile(tmp_path, monkeypatch)
    profile = Path(os.environ["PITWALL_PROFILE"])
    values = json.loads(profile.read_text(encoding="utf-8"))
    values["speech"] = {"enabled": True, "engine": engine_id}
    profile.write_text(json.dumps(values), encoding="utf-8")

    seen: dict[str, object] = {}

    async def fake_serve(engine, hub, _store, live, **kwargs) -> None:  # type: ignore[no-untyped-def]
        live.close()
        hub.audio_clients.add(object())
        call = make_call("live-start")
        hub.speak(call)
        t0 = time.time()
        engine.dispatcher.metrics.note_handoff(call.id, t0)
        assert speaker.on_audio is not None
        assert speaker.on_spoken is not None
        assert speaker.on_dropped is not None
        speaker.on_audio(call.id, call.priority, _small_wav())
        speaker.on_spoken(call.id, t0 + 0.05)
        seen["latency"] = engine.dispatcher.metrics.summary()["tts_ms"]["p99"]
        seen["stream"] = hub.streams_audio
        seen["name"] = engine.speaker_name

    monkeypatch.setattr(cli, "_serve", fake_serve)
    assert cli.cmd_start(_start_args()) == 0
    assert seen["latency"] == pytest.approx(50.0)
    assert seen["stream"] is True
    assert seen["name"] == speaker.name
    assert speaker.closed


def test_dashboard_latency_values_use_metrics() -> None:
    metrics = Metrics()
    metrics.queue_source = lambda: (3, 0.75)
    metrics.queue_wait_ms.add(17.0)
    metrics.tts_ms.add(31.0)
    metrics.packet_to_ws_ms.add(42.0)
    from pitwall.config.loader import ConfigStore

    payload = state_payload(
        SessionState().snapshot(1.0),
        settings=ConfigStore().current(),
        metrics=metrics,
        quiet=False,
    )
    assert payload["latency"] == {
        "queue_count": 3,
        "queue_oldest_s": 0.75,
        "queue_wait_p99_ms": 17.0,
        "tts_p99_ms": 31.0,
        "packet_to_ws_p99_ms": 42.0,
    }
