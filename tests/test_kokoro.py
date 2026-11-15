from __future__ import annotations

import io
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pitwall.audio.kokoro_tts import KOKORO_MODEL, KOKORO_VOICES, make_kokoro_speaker
from pitwall.audio.speaker import ENGINES, EngineSpec, make_speaker
from pitwall.audio.wav_speaker import WavSpeaker, radio_blip, to_wav
from pitwall.config.models import SpeechSettings
from tests.audio_contract import (
    FakePlayer,
    assert_pitwall_wav,
    check_speaker_contract,
    make_call,
)


@pytest.fixture
def kokoro_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    env = SimpleNamespace(
        dir=tmp_path / "voices",
        voices={"bf_emma", "af_bella"},
        creates=[],
        sessions=[],
        logger_severities=[],
        preloads=[],
        cuda=True,
    )
    model_dir = env.dir / "kokoro"
    model_dir.mkdir(parents=True)
    (model_dir / KOKORO_MODEL).write_bytes(b"model")
    (model_dir / KOKORO_VOICES).write_bytes(b"voices")

    class FakeSession:
        def __init__(self, path: str, providers: list[object]) -> None:
            env.sessions.append((path, providers))
            self._providers = providers

        def get_providers(self) -> list[str]:
            return [p[0] if isinstance(p, tuple) else p for p in self._providers]

    class FakeKokoro:
        voices = env.voices

        def __init__(self, session: FakeSession, voices_path: str) -> None:
            self.session = session

        @classmethod
        def from_session(cls, session: FakeSession, voices_path: str) -> FakeKokoro:
            return cls(session, voices_path)

        def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[np.ndarray, int]:
            env.creates.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
            if text == "bad":
                raise RuntimeError("render failed")
            return np.full(4800, 0.5, dtype=np.float32), 24000

    onnxruntime = SimpleNamespace(
        get_available_providers=lambda: (
            (["CUDAExecutionProvider"] if env.cuda else []) + ["CPUExecutionProvider"]
        ),
        set_default_logger_severity=env.logger_severities.append,
        InferenceSession=FakeSession,
        preload_dlls=lambda: env.preloads.append(True),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=FakeKokoro))
    return env


def _settings(env: SimpleNamespace, **overrides: object) -> SpeechSettings:
    return SpeechSettings(engine="kokoro", voices_dir=str(env.dir), **overrides)


def _identity(speaker: WavSpeaker) -> str:
    return speaker._renderer.cache_identity


def _pcm(wav: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")


def test_kokoro_speaker_passes_contract(kokoro_env: SimpleNamespace) -> None:
    player = FakePlayer()
    speaker = make_kokoro_speaker(_settings(kokoro_env), player)
    check_speaker_contract(speaker, player)
    speaker.close()


def test_render_is_mono_16_bit_24_khz(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    wav, seconds = speaker.render("Box this lap.", 2)
    assert assert_pitwall_wav(wav) == pytest.approx(seconds)
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getframerate() == 24000
    speaker.close()


def test_radio_click_is_prepended(kokoro_env: SimpleNamespace) -> None:
    with_click = make_kokoro_speaker(_settings(kokoro_env, radio_click=True), FakePlayer())
    without = make_kokoro_speaker(_settings(kokoro_env, radio_click=False), FakePlayer())
    on = _pcm(with_click.render("Box this lap.", 2)[0])
    off = _pcm(without.render("Box this lap.", 2)[0])
    blip = _pcm(to_wav(radio_blip(24000), 24000))
    assert len(on) - len(off) == len(blip)
    assert np.array_equal(on[: len(blip)], blip)
    with_click.close()
    without.close()


def test_create_gets_voice_lang_and_priority_speed(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(
        _settings(kokoro_env, kokoro_voice="af_bella", kokoro_lang="en-us"), FakePlayer()
    )
    kokoro_env.creates.clear()
    for priority in (1, 2, 3):
        speaker.render(f"line {priority}", priority)
    assert [c["voice"] for c in kokoro_env.creates] == ["af_bella"] * 3
    assert {c["lang"] for c in kokoro_env.creates} == {"en-us"}
    assert [c["speed"] for c in kokoro_env.creates] == pytest.approx([1.1, 1.0, 0.95])
    speaker.close()


def test_ers_is_spelled_out_for_kokoro(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    kokoro_env.creates.clear()
    speaker.render("ERS fault. No DRS", 2)
    assert kokoro_env.creates[0]["text"] == "E R S fault. No DRS"
    speaker.close()


def test_speed_is_clamped_to_kokoro_range(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env, kokoro_speed=3.0), FakePlayer())
    kokoro_env.creates.clear()
    speaker.render("normal", 2)
    speaker.render("urgent", 1)
    assert [c["speed"] for c in kokoro_env.creates] == [2.0, 2.0]
    speaker.close()


def test_cache_identity_follows_voice_speed_and_model(kokoro_env: SimpleNamespace) -> None:
    base = _identity(make_kokoro_speaker(_settings(kokoro_env), FakePlayer()))
    voice = _identity(
        make_kokoro_speaker(_settings(kokoro_env, kokoro_voice="af_bella"), FakePlayer())
    )
    speed = _identity(make_kokoro_speaker(_settings(kokoro_env, kokoro_speed=1.2), FakePlayer()))
    assert len({base, voice, speed}) == 3
    (kokoro_env.dir / "kokoro" / KOKORO_MODEL).write_bytes(b"larger model file")
    assert _identity(make_kokoro_speaker(_settings(kokoro_env), FakePlayer())) != base


def test_cuda_session_uses_default_cudnn_search(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    assert kokoro_env.sessions[-1][1] == [
        ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}),
        "CPUExecutionProvider",
    ]
    assert kokoro_env.preloads == [True]
    assert kokoro_env.logger_severities == [3]
    assert speaker.name == "kokoro (bf_emma, cuda)"
    speaker.close()


@pytest.mark.parametrize(("cuda_is_available", "device"), [(True, "cpu"), (False, "auto")])
def test_cpu_session_when_cuda_is_forced_off_or_missing(
    kokoro_env: SimpleNamespace, cuda_is_available: bool, device: str
) -> None:
    kokoro_env.cuda = cuda_is_available
    speaker = make_kokoro_speaker(_settings(kokoro_env, kokoro_device=device), FakePlayer())
    assert kokoro_env.sessions[-1][1] == ["CPUExecutionProvider"]
    assert kokoro_env.preloads == []
    assert speaker.name == "kokoro (bf_emma, cpu)"
    speaker.close()


def test_missing_model_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=KOKORO_MODEL):
        make_kokoro_speaker(SpeechSettings(engine="kokoro", voices_dir=str(tmp_path)))


def test_missing_model_falls_back_to_piper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setitem(ENGINES, "piper", EngineSpec("piper", lambda _s: sentinel, "null"))
    assert make_speaker(SpeechSettings(engine="kokoro", voices_dir=str(tmp_path))) is sentinel


def test_unknown_voice_falls_back_to_piper(
    kokoro_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    monkeypatch.setitem(ENGINES, "piper", EngineSpec("piper", lambda _s: sentinel, "null"))
    assert make_speaker(_settings(kokoro_env, kokoro_voice="xx_nobody")) is sentinel


def test_render_failure_after_startup_drops_call(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    dropped = threading.Event()
    speaker.on_dropped = lambda _call_id: dropped.set()
    speaker.speak(make_call("bad-call", text="bad"))
    assert dropped.wait(2.0)
    speaker.close()
