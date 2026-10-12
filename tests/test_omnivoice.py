from __future__ import annotations

import asyncio
import base64
import io
import json
import socket
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pitwall.audio.dispatcher import Call
from pitwall.audio.omnivoice_tts import (
    OmniVoiceError,
    OmniVoiceSpeaker,
    fetch_model_id,
    make_omnivoice_speaker,
    make_omnivoice_synth,
    omnivoice_cache_key,
    register_voice,
)
from pitwall.audio.piper_tts import (
    NullPlayer,
    PiperSpeaker,
    default_player,
    to_wav,
)
from pitwall.config.models import SpeechSettings

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
            elif self.path == "/v1/audio/voices":
                self._send(200, b'{"name":"test-voice","status":"registered"}')
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


def _settings(url: str, tmp_path: Path, **updates: object) -> SpeechSettings:
    wav_path = tmp_path / "reference.wav"
    text_path = tmp_path / "reference.txt"
    wav_path.write_bytes(SMALL_WAV)
    text_path.write_text("  reference words \n", encoding="utf-8")
    values: dict[str, object] = {
        "engine": "omnivoice",
        "omnivoice_url": url,
        "omnivoice_voice": "test-voice",
        "omnivoice_seed": 17,
        "omnivoice_language": "English",
        "omnivoice_ref_wav": str(wav_path),
        "omnivoice_ref_text": str(text_path),
        "omnivoice_timeout_s": 1.0,
        "voices_dir": str(tmp_path / "voices"),
        "radio_click": False,
    }
    values.update(updates)
    return SpeechSettings(**values)


def _wait_for(predicate: Any, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met")


def test_synth_posts_exact_request_and_returns_wav(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    settings = _settings(url, tmp_path)

    wav, seconds = make_omnivoice_synth(settings)("Box this lap.")

    assert wav == SMALL_WAV
    assert seconds == pytest.approx(0.01)
    assert state.requests == [
        (
            "/v1/audio/speech",
            {
                "input": "Box this lap.",
                "voice": "test-voice",
                "language": "English",
                "response_format": "wav",
                "seed": 17,
            },
        )
    ]
    assert type(state.requests[0][1]["seed"]) is int


def test_synth_prepends_radio_click(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    settings = _settings(url, tmp_path, radio_click=True)

    wav, seconds = make_omnivoice_synth(settings)("Box this lap.")

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


def test_register_voice_posts_reference_and_returns_identity(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, state = fake_server
    settings = _settings(url, tmp_path)

    identity = register_voice(settings)

    request = state.requests[-1]
    assert request[0] == "/v1/audio/voices"
    assert request[1]["name"] == "test-voice"
    assert request[1]["ref_text"] == "reference words"
    assert base64.b64decode(request[1]["wav_b64"]) == SMALL_WAV
    assert identity


def test_register_voice_reports_missing_reference_files(tmp_path: Path) -> None:
    settings = SpeechSettings(
        omnivoice_voice="voice",
        omnivoice_ref_wav=str(tmp_path / "missing.wav"),
        omnivoice_ref_text=str(tmp_path / "missing.txt"),
    )

    with pytest.raises(OmniVoiceError, match="reference WAV not found"):
        register_voice(settings)


def test_fetch_model_id_reads_first_model(fake_server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, _ = fake_server
    assert fetch_model_id(_settings(url, tmp_path)) == "voice-model.gguf"


def test_make_speaker_falls_back_to_null_when_omnivoice_and_piper_are_unavailable(
    tmp_path: Path,
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()

    from pitwall.audio.speaker import NullSpeaker, make_speaker

    speaker = make_speaker(
        SpeechSettings(
            engine="omnivoice",
            omnivoice_url=url,
            voices_dir=str(tmp_path),
        )
    )

    assert isinstance(speaker, NullSpeaker)
    speaker.close()


def test_make_speaker_falls_through_to_piper(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.speaker as speaker_module

    sentinel = PiperSpeaker(lambda text: (b"wav", 0.0), NullPlayer(), label="sentinel")
    monkeypatch.setattr(speaker_module, "make_piper_speaker", lambda settings: sentinel)
    speaker = speaker_module.make_speaker(
        SpeechSettings(
            engine="omnivoice",
            omnivoice_url="http://127.0.0.1:1",
            voices_dir=str(tmp_path),
        )
    )

    assert speaker is sentinel
    sentinel.close()


def test_omnivoice_cache_key_tracks_voice_configuration() -> None:
    settings = SpeechSettings(
        omnivoice_voice="voice",
        omnivoice_voice_version="1",
        omnivoice_seed=42,
        omnivoice_language="English",
    )
    baseline = omnivoice_cache_key(settings, "model.gguf", "ref")

    assert omnivoice_cache_key(settings, "model.gguf", "ref") == baseline
    changes = [
        ({"omnivoice_voice_version": "2"}, "model.gguf", "ref"),
        ({"omnivoice_seed": 43}, "model.gguf", "ref"),
        ({"omnivoice_voice": "other"}, "model.gguf", "ref"),
        ({}, "model.gguf", "other-ref"),
        ({}, "other-model.gguf", "ref"),
        ({"omnivoice_language": "French"}, "model.gguf", "ref"),
    ]
    for update, model_id, ref_identity in changes:
        changed = settings.model_copy(update=update)
        assert omnivoice_cache_key(changed, model_id, ref_identity) != baseline


class FakePlayer:
    def play(self, wav: bytes) -> None:
        pass

    def stop(self) -> None:
        pass


def test_speaker_uses_fallback_then_background_cache_and_reuses_disk(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    omnivoice_calls: list[str] = []
    fallback_calls: list[str] = []
    omnivoice_wav = to_wav(np.zeros(480, dtype=np.float32), 24_000)

    def omnivoice(text: str) -> tuple[bytes, float]:
        omnivoice_calls.append(text)
        entered.set()
        assert release.wait(2)
        return omnivoice_wav, 0.02

    def fallback(text: str) -> tuple[bytes, float]:
        fallback_calls.append(text)
        return b"piper", 0.01

    speaker = OmniVoiceSpeaker(
        omnivoice,
        {2: fallback},
        FakePlayer(),
        "omnivoice",
        tmp_path / "phrases",
        "key",
    )
    try:
        assert speaker.render("first line") == (b"piper", 0.01)
        assert entered.wait(1)
        assert speaker.render("first line") == (b"piper", 0.01)
        assert len(omnivoice_calls) == 1
        release.set()
        path = speaker._phrase_path((0, "first line"))
        assert path is not None
        _wait_for(path.is_file)
        assert speaker.render("first line") == (omnivoice_wav, 0.02)
    finally:
        release.set()
        speaker.close()

    restarted_calls: list[str] = []

    def should_not_render(text: str) -> tuple[bytes, float]:
        restarted_calls.append(text)
        return b"unexpected", 0.0

    restarted = OmniVoiceSpeaker(
        should_not_render,
        {2: fallback},
        FakePlayer(),
        "omnivoice",
        tmp_path / "phrases",
        "key",
    )
    try:
        assert restarted.render("first line") == (omnivoice_wav, 0.02)
        assert restarted_calls == []
        assert fallback_calls == ["first line", "first line"]
    finally:
        restarted.close()


def test_speaker_without_fallback_renders_synchronously_and_caches(tmp_path: Path) -> None:
    calls: list[str] = []

    def synth(text: str) -> tuple[bytes, float]:
        calls.append(text)
        return b"omnivoice", 0.5

    speaker = OmniVoiceSpeaker(synth, None, FakePlayer(), "omnivoice", tmp_path, "key")
    try:
        assert speaker.render("line") == (b"omnivoice", 0.5)
        assert speaker.render("line", 1) == (b"omnivoice", 0.5)
        assert calls == ["line"]
    finally:
        speaker.close()


def test_synchronous_failure_uses_last_resort_with_original_call_and_callback(
    tmp_path: Path,
) -> None:
    received: list[Call] = []
    cancelled: list[str] = []
    spoken = threading.Event()
    spoken_ids: list[str] = []

    class FakeLastResort:
        name = "null"
        speaks_audio = True
        on_spoken = None
        closed = False

        def speak(self, call: Call) -> None:
            received.append(call)
            if self.on_spoken is not None:
                self.on_spoken(call.id, time.time())

        def cancel(self, call_id: str) -> None:
            cancelled.append(call_id)

        def close(self) -> None:
            self.closed = True

    def fail(text: str) -> tuple[bytes, float]:
        raise OmniVoiceError("synthesis failed")

    last_resort = FakeLastResort()
    speaker = OmniVoiceSpeaker(
        fail,
        None,
        FakePlayer(),
        "omnivoice",
        tmp_path,
        "key",
        last_resort=last_resort,
    )

    def on_spoken(call_id: str, _timestamp: float) -> None:
        spoken_ids.append(call_id)
        spoken.set()

    speaker.on_spoken = on_spoken
    call = Call(
        id="original-id",
        rule_id="test",
        priority=2,
        text="line",
        tags=[],
        deadline_ms=1000,
        lap=0,
        t=0.0,
        trigger_t=0.0,
    )
    try:
        speaker.speak(call)
        assert spoken.wait(1.0)
        assert received == [call]
        assert received[0] is call
        assert spoken_ids == ["original-id"]
        speaker.cancel("cancel-id")
    finally:
        speaker.close()
    assert cancelled == ["cancel-id"]
    assert last_resort.closed


def test_warm_deduplicates_and_uses_only_omnivoice(tmp_path: Path) -> None:
    calls: list[str] = []
    fallback_calls: list[str] = []

    def omnivoice(text: str) -> tuple[bytes, float]:
        calls.append(text)
        return SMALL_WAV, 0.01

    def fallback(text: str) -> tuple[bytes, float]:
        fallback_calls.append(text)
        return b"piper", 0.1

    speaker = OmniVoiceSpeaker(omnivoice, {2: fallback}, FakePlayer(), "omnivoice", tmp_path, "key")
    existing = speaker._phrase_path((0, "cached"))
    assert existing is not None
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(SMALL_WAV)
    try:
        assert speaker.warm([("cached", 2), ("line", 1), ("line", 3)]) == 2
        assert calls == ["line"]
        assert fallback_calls == []
        line_path = speaker._phrase_path((0, "line"))
        assert line_path is not None and line_path.read_bytes() == SMALL_WAV
    finally:
        speaker.close()


def test_make_omnivoice_speaker_is_piper_subclass_and_uses_null_player(
    fake_server, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.omnivoice_tts as omnivoice_module

    url, _ = fake_server

    def no_piper(settings: SpeechSettings) -> dict[int, Any]:
        raise FileNotFoundError("no local Piper model")

    monkeypatch.setattr(omnivoice_module, "make_piper_tone_synths", no_piper)
    speaker = make_omnivoice_speaker(_settings(url, tmp_path))
    try:
        assert isinstance(speaker, PiperSpeaker)
        assert isinstance(speaker._player, NullPlayer)
        if sys.platform != "win32":
            from pitwall.audio.speaker import NullSpeaker

            assert isinstance(default_player(), NullPlayer)
            assert isinstance(speaker._last_resort, NullSpeaker)
    finally:
        speaker.close()


def test_make_omnivoice_speaker_with_piper_has_no_last_resort(
    fake_server, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.omnivoice_tts as omnivoice_module

    url, _ = fake_server

    def fallback(text: str) -> tuple[bytes, float]:
        return SMALL_WAV, 0.01

    monkeypatch.setattr(
        omnivoice_module,
        "make_piper_tone_synths",
        lambda settings: {1: fallback, 2: fallback, 3: fallback},
    )
    speaker = make_omnivoice_speaker(_settings(url, tmp_path))
    try:
        assert speaker._last_resort is None
    finally:
        speaker.close()


def test_voice_warm_auto_uses_configured_engine_and_explicit_piper(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    import pitwall.audio.omnivoice_tts as omnivoice_module
    import pitwall.audio.piper_tts as piper_module
    import pitwall.cli.voice as voice_module
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
    chosen: list[str] = []
    speakers: list[FakeSpeaker] = []

    def make(name: str) -> FakeSpeaker:
        chosen.append(name)
        speaker = FakeSpeaker()
        speakers.append(speaker)
        return speaker

    monkeypatch.setattr(
        omnivoice_module, "make_omnivoice_speaker", lambda speech: make("omnivoice")
    )
    monkeypatch.setattr(piper_module, "make_piper_speaker", lambda speech: make("piper"))
    assert main(["voice", "warm"]) == 0
    assert main(["voice", "warm", "--engine", "piper"]) == 0
    assert chosen == ["omnivoice", "piper"]
    assert all(speaker.closed for speaker in speakers)
    assert "cached " in capsys.readouterr().out


def test_voice_say_uses_configured_engine_and_accepts_omnivoice(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

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


def test_sync_render_regenerates_corrupt_cached_wav(tmp_path: Path) -> None:
    calls: list[str] = []

    def synth(text: str) -> tuple[bytes, float]:
        calls.append(text)
        return SMALL_WAV, 0.01

    speaker = OmniVoiceSpeaker(synth, None, FakePlayer(), "omnivoice", tmp_path, "key")
    path = speaker._phrase_path((0, "line"))
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SMALL_WAV[:-8])
    try:
        assert speaker.render("line") == (SMALL_WAV, 0.01)
        assert calls == ["line"]
        assert path.read_bytes() == SMALL_WAV
    finally:
        speaker.close()


def test_warm_regenerates_corrupt_cache_and_removes_temporary_files(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def synth(text: str) -> tuple[bytes, float]:
        calls.append(text)
        return SMALL_WAV, 0.01

    speaker = OmniVoiceSpeaker(synth, None, FakePlayer(), "omnivoice", tmp_path, "key")
    path = speaker._phrase_path((0, "line"))
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SMALL_WAV[:-8])
    try:
        assert speaker.warm([("line", 2)]) == 1
        assert calls == ["line"]
        assert path.read_bytes() == SMALL_WAV
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        speaker.close()


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
