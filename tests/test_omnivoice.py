from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import wave
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from pitwall.audio.omnivoice_tts import (
    OmniVoiceError,
    fetch_model_id,
    make_omnivoice_speaker,
    make_omnivoice_synth,
    omnivoice_cache_key,
)
from pitwall.audio.piper_tts import to_wav
from pitwall.audio.wav_speaker import NullPlayer, WavSpeaker, default_player
from pitwall.config.models import SpeechSettings
from tests.audio_contract import FakePlayer, assert_pitwall_wav, make_call

SMALL_WAV = to_wav(np.zeros(240, dtype=np.float32), 24_000)


@dataclass
class FakeServerState:
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    speech_status: int = 200
    speech_delay_s: float = 0.0


@pytest.fixture
def fake_server() -> tuple[str, FakeServerState]:
    state = FakeServerState()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def do_GET(self) -> None:
            state.requests.append((self.path, {}))
            self._send(200, b'{"object":"list","data":[{"id":"voice-model.gguf"}]}')

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body)
            state.requests.append((self.path, payload))
            if self.path == "/v1/audio/speech":
                if state.speech_delay_s:
                    time.sleep(state.speech_delay_s)
                if state.speech_status != 200:
                    self._send(state.speech_status, b'{"error":"synthesis failed"}')
                else:
                    self._send(200, SMALL_WAV, "audio/wav")
            else:
                self._send(404, b'{"error":"not found"}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


OLD_PROFILE_FIELDS: dict[str, object] = {
    "omnivoice_voice": "test-voice",
    "omnivoice_seed": 17,
    "omnivoice_language": "French",
    "omnivoice_voice_version": "9",
    "omnivoice_ref_wav": "missing.wav",
    "omnivoice_ref_text": "missing.txt",
}


def _settings(url: str, tmp_path: Path, **updates: object) -> SpeechSettings:
    values: dict[str, object] = {
        "engine": "omnivoice",
        "omnivoice_url": url,
        "omnivoice_timeout_s": 1.0,
        "voices_dir": str(tmp_path / "voices"),
        "radio_click": False,
    }
    values.update(updates)
    return SpeechSettings(**values)


def _wait_for(predicate, timeout_s: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met")


@pytest.mark.parametrize("deprecated", [{}, OLD_PROFILE_FIELDS])
def test_synth_posts_text_only_and_returns_wav(
    fake_server, tmp_path: Path, deprecated: dict[str, object]
) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    settings = _settings(url, tmp_path, **deprecated)

    wav, seconds = make_omnivoice_synth(settings)("Box this lap.")

    assert wav == SMALL_WAV
    assert seconds == pytest.approx(0.01)
    assert state.requests == [
        ("/v1/audio/speech", {"input": "Box this lap.", "response_format": "wav"})
    ]


def test_synth_prepends_radio_click(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    wav, seconds = make_omnivoice_synth(_settings(url, tmp_path, radio_click=True))("Box this lap.")

    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getframerate() == 24_000
        assert seconds == pytest.approx(reader.getnframes() / reader.getframerate())
        assert seconds > 0.01


@pytest.mark.parametrize("failure", ["status", "timeout"])
def test_synth_wraps_server_failure_and_timeout(fake_server, tmp_path: Path, failure: str) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    timeout_s = 1.0
    if failure == "status":
        state.speech_status = 500
    else:
        state.speech_delay_s = 0.5
        timeout_s = 0.2

    with pytest.raises(OmniVoiceError) as error:
        make_omnivoice_synth(_settings(url, tmp_path), timeout_s=timeout_s)("test")
    if failure == "status":
        assert "synthesis failed" in str(error.value)


def test_fetch_model_id_reads_first_model(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    assert fetch_model_id(_settings(url, tmp_path)) == "voice-model.gguf"


def test_make_omnivoice_speaker_uses_fallback_without_piper(
    fake_server, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.omnivoice_tts as omnivoice_module

    url, _ = fake_server
    monkeypatch.setattr(
        omnivoice_module,
        "make_piper_tone_synths",
        lambda settings: (_ for _ in ()).throw(FileNotFoundError("no Piper model")),
    )
    speaker = make_omnivoice_speaker(_settings(url, tmp_path), FakePlayer())
    try:
        assert isinstance(speaker, WavSpeaker)
        assert speaker._renderer.fallback_tones is None
        if __import__("sys").platform != "win32":
            assert isinstance(default_player(), NullPlayer)
            assert speaker._fallback is not None
    finally:
        speaker.close()


def test_make_omnivoice_speaker_uses_piper_provisional_audio(
    fake_server, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.omnivoice_tts as omnivoice_module

    url, state = fake_server
    tones = {priority: lambda _text: (SMALL_WAV, 0.01) for priority in (1, 2, 3)}
    monkeypatch.setattr(omnivoice_module, "make_piper_tone_synths", lambda settings: tones)
    speaker = make_omnivoice_speaker(_settings(url, tmp_path, **OLD_PROFILE_FIELDS), FakePlayer())
    try:
        assert state.requests == [("/v1/models", {})]
        assert speaker._fallback is None
        assert speaker._renderer.provisional("test", 2) == (SMALL_WAV, 0.01)
    finally:
        speaker.close()


def test_omnivoice_speaker_streams_contract_wav(fake_server, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.omnivoice_tts as omnivoice_module

    url, _ = fake_server
    tones = {priority: lambda _text: (SMALL_WAV, 0.01) for priority in (1, 2, 3)}
    monkeypatch.setattr(omnivoice_module, "make_piper_tone_synths", lambda settings: tones)
    speaker = make_omnivoice_speaker(_settings(url, tmp_path), FakePlayer())
    events: list[str] = []
    speaker.on_audio = lambda _call_id, _priority, wav: (
        assert_pitwall_wav(wav),
        events.append("audio"),
    )
    speaker.on_spoken = lambda _call_id, _time: events.append("spoken")
    try:
        speaker.speak(make_call("omnivoice-contract", text="Box this lap."))
        _wait_for(lambda: events.count("spoken") == 1)
        assert events == ["audio", "spoken"]
    finally:
        speaker.close()


def test_omnivoice_cache_key_tracks_client_inputs_only() -> None:
    settings = SpeechSettings(radio_click=True)
    baseline = omnivoice_cache_key(settings, "model.gguf")

    assert omnivoice_cache_key(settings, "model.gguf") == baseline
    assert omnivoice_cache_key(settings, "other-model.gguf") != baseline
    changed = settings.model_copy(update={"radio_click": False})
    assert omnivoice_cache_key(changed, "model.gguf") != baseline


def test_old_profile_omnivoice_fields_are_ignored(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pitwall.config.loader import ConfigStore

    profile = tmp_path / "profile.yaml"
    profile.write_text(json.dumps({"speech": OLD_PROFILE_FIELDS}), encoding="utf-8")
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))

    speech = ConfigStore().current().speech

    assert not any(hasattr(speech, name) for name in OLD_PROFILE_FIELDS)


def test_voice_warm_auto_uses_configured_engine_and_explicit_engine(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.speaker as speaker_module
    import pitwall.cli.voice as voice_module
    from pitwall.audio.speaker import EngineSpec
    from pitwall.cli import main

    class FakeSpeaker:
        name = "fake"

        def __init__(self) -> None:
            self.closed = False

        def warm(self, phrases: list[tuple[str, int]]) -> int:
            return len(phrases)

        def close(self) -> None:
            self.closed = True

    settings = SpeechSettings(engine="omnivoice")
    store = SimpleNamespace(current=lambda: SimpleNamespace(speech=settings))
    monkeypatch.setattr(voice_module, "ConfigStore", lambda: store)
    seen: list[str] = []
    speakers: list[FakeSpeaker] = []

    def make_factory(engine: str):
        def factory(_speech: SpeechSettings) -> FakeSpeaker:
            seen.append(engine)
            speaker = FakeSpeaker()
            speakers.append(speaker)
            return speaker

        return factory

    monkeypatch.setattr(
        speaker_module,
        "ENGINES",
        {
            "omnivoice": EngineSpec("omnivoice", make_factory("omnivoice"), "piper"),
            "piper": EngineSpec("piper", make_factory("piper"), None),
        },
    )
    assert main(["voice", "warm"]) == 0
    assert main(["voice", "warm", "--engine", "piper"]) == 0
    assert seen == ["omnivoice", "piper"]
    assert all(speaker.closed for speaker in speakers)
    assert "cached " in capsys.readouterr().out


def test_voice_warm_rejects_non_warmable_engine(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import pitwall.cli.voice as voice_module
    from pitwall.cli import main

    settings = SpeechSettings(engine="null")
    store = SimpleNamespace(current=lambda: SimpleNamespace(speech=settings))
    monkeypatch.setattr(voice_module, "ConfigStore", lambda: store)

    assert main(["voice", "warm"]) == 1
    assert "does not support phrase warming" in capsys.readouterr().out


def test_voice_warm_reports_unknown_configured_engine(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import pitwall.cli.voice as voice_module
    from pitwall.cli import main

    settings = SpeechSettings(engine="unknown")
    store = SimpleNamespace(current=lambda: SimpleNamespace(speech=settings))
    monkeypatch.setattr(voice_module, "ConfigStore", lambda: store)

    assert main(["voice", "warm"]) == 1
    assert capsys.readouterr().out.strip() == "unknown speech engine: unknown"


def test_voice_warm_unreachable_omnivoice_does_not_warm_piper(
    monkeypatch, tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.speaker as speaker_module
    import pitwall.cli.voice as voice_module
    from pitwall.audio.speaker import EngineSpec
    from pitwall.cli import main

    cache_dir = tmp_path / "voices" / ".phrases"
    piper_created: list[bool] = []

    class FakePiperSpeaker:
        name = "piper"

        def warm(self, phrases: list[tuple[str, int]]) -> int:
            cache_dir.mkdir(parents=True)
            (cache_dir / "cached.wav").write_bytes(b"wav")
            return len(phrases)

        def close(self) -> None:
            pass

    def make_piper(_speech: SpeechSettings) -> FakePiperSpeaker:
        piper_created.append(True)
        return FakePiperSpeaker()

    piper_spec = speaker_module.ENGINES["piper"]
    monkeypatch.setitem(
        speaker_module.ENGINES,
        "piper",
        EngineSpec("piper", make_piper, piper_spec.fallback),
    )
    settings = SpeechSettings(
        engine="auto",
        omnivoice_url="http://127.0.0.1:1",
        omnivoice_timeout_s=0.2,
        voices_dir=str(tmp_path / "voices"),
    )
    store = SimpleNamespace(current=lambda: SimpleNamespace(speech=settings))
    monkeypatch.setattr(voice_module, "ConfigStore", lambda: store)

    assert main(["voice", "warm", "--engine", "omnivoice"]) == 1
    assert "omnivoice unavailable:" in capsys.readouterr().out
    assert piper_created == []
    assert not cache_dir.exists()


def test_voice_say_uses_configured_engine_and_accepts_omnivoice(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.speaker as speaker_module
    import pitwall.cli.voice as voice_module
    from pitwall.audio.speaker import NullSpeaker
    from pitwall.cli import main

    settings = SpeechSettings(engine="omnivoice")
    store = SimpleNamespace(current=lambda: SimpleNamespace(speech=settings))
    monkeypatch.setattr(voice_module, "ConfigStore", lambda: store)
    seen: list[SpeechSettings] = []

    def make_speaker(speech: SpeechSettings) -> NullSpeaker:
        seen.append(speech)
        return NullSpeaker()

    monkeypatch.setattr(speaker_module, "make_speaker", make_speaker)
    assert main(["voice", "say"]) == 0
    assert seen[-1].engine == "omnivoice"
    assert "engine=omnivoice" in capsys.readouterr().out
    assert main(["voice", "say", "--engine", "omnivoice", "test"]) == 0
    assert seen[-1].engine == "omnivoice"


def test_voice_warm_replays_deduplicated_calls_against_omnivoice(
    fake_server, tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    from pitwall.clock import VirtualClock
    from pitwall.engine import build_engine, run_replay

    url, state = fake_server
    wav_path = tmp_path / "reference.wav"
    text_path = tmp_path / "reference.txt"
    wav_path.write_bytes(SMALL_WAV)
    text_path.write_text("reference words", encoding="utf-8")
    recording_dir = tmp_path / "recordings"
    recording_dir.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        json.dumps(
            {
                "persistence": {"enabled": False},
                "recording": {"directory": str(recording_dir)},
                "speech": {
                    "engine": "omnivoice",
                    "omnivoice_url": url,
                    "omnivoice_voice": "test-voice",
                    "omnivoice_ref_wav": str(wav_path),
                    "omnivoice_ref_text": str(text_path),
                    "omnivoice_timeout_s": 1.0,
                    "voices_dir": str(tmp_path / "voices"),
                    "radio_click": False,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))
    monkeypatch.chdir(tmp_path)

    from .race_synth import RaceSpec, race_stream
    from .synth import write_packet_stream

    recording = write_packet_stream(recording_dir / "race.f1bin", race_stream(RaceSpec(laps=8)))
    expected_engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        decision_log_path=tmp_path / "expected.jsonl",
    )
    try:
        _, calls = asyncio.run(run_replay(recording, expected_engine, None))
    finally:
        expected_engine.dispatcher.log.close()
    expected = list(
        dict.fromkeys(
            text
            for text, _ in sorted(
                {(call.text, call.priority) for call in calls},
                key=lambda phrase: (phrase[1], phrase[0]),
            )
        )
    )
    assert expected

    import pitwall.audio.omnivoice_tts as omnivoice_module
    from pitwall.cli import main

    monkeypatch.setattr(
        omnivoice_module,
        "make_piper_tone_synths",
        lambda settings: (_ for _ in ()).throw(FileNotFoundError("no Piper model")),
    )
    assert main(["voice", "warm", "--engine", "omnivoice", "--replay", str(recording)]) == 0
    speech = [payload["input"] for path, payload in state.requests if path == "/v1/audio/speech"]

    assert speech == expected
    assert not (recording_dir / "decisions.jsonl").exists()
    assert not (tmp_path / "pitwall.sqlite").exists()
    assert f"cached {len(expected)} phrases" in capsys.readouterr().out
