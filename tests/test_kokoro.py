from __future__ import annotations

import io
import json
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pitwall.audio.kokoro_tts import (
    GPU_MEM_LIMIT_MB,
    KOKORO_MODEL,
    KOKORO_VOICES,
    PRESIZE_TEXT,
    make_kokoro_speaker,
)
from pitwall.audio.piper_tts import voice_path
from pitwall.audio.speaker import ENGINES, EngineSpec, auto_engine, make_speaker
from pitwall.audio.wav_speaker import WavSpeaker, radio_blip, to_wav
from pitwall.cli import main
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
        voices={"af_heart", "af_bella"},
        creates=[],
        sessions=[],
        session_options=[],
        logger_severities=[],
        preloads=[],
        cuda=True,
    )
    model_dir = env.dir / "kokoro"
    model_dir.mkdir(parents=True)
    (model_dir / KOKORO_MODEL).write_bytes(b"model")
    (model_dir / KOKORO_VOICES).write_bytes(b"voices")

    class FakeSession:
        def __init__(
            self, path: str, sess_options: object = None, providers: list[object] | None = None
        ) -> None:
            env.sessions.append((path, providers))
            env.session_options.append(sess_options)
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
            device = self.session.get_providers()[0]
            env.creates.append(
                {"text": text, "voice": voice, "speed": speed, "lang": lang, "device": device}
            )
            if text == "bad":
                raise RuntimeError("render failed")
            if text == "gpu-bad" and device == "CUDAExecutionProvider":
                raise RuntimeError("gpu failed")
            return np.full(4800, 0.5, dtype=np.float32), 24000

    onnxruntime = SimpleNamespace(
        get_available_providers=lambda: (
            (["CUDAExecutionProvider"] if env.cuda else []) + ["CPUExecutionProvider"]
        ),
        set_default_logger_severity=env.logger_severities.append,
        InferenceSession=FakeSession,
        SessionOptions=SimpleNamespace,
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


def test_default_voice_is_american_english() -> None:
    settings = SpeechSettings()
    assert settings.kokoro_voice == "af_heart"
    assert settings.kokoro_lang == "en-us"


def test_create_gets_voice_lang_and_priority_speed(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(
        _settings(kokoro_env, kokoro_voice="af_bella", kokoro_lang="en-gb"), FakePlayer()
    )
    kokoro_env.creates.clear()
    for priority in (1, 2, 3):
        speaker.render(f"line {priority}", priority)
    assert [c["voice"] for c in kokoro_env.creates] == ["af_bella"] * 3
    assert {c["lang"] for c in kokoro_env.creates} == {"en-gb"}
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


@pytest.mark.parametrize(
    "overrides",
    [{"warm_phrases": 10}, {"omnivoice_url": "http://elsewhere:9000"}, {"kokoro_device": "cpu"}],
)
def test_cache_identity_ignores_device_and_service_settings(
    kokoro_env: SimpleNamespace, overrides: dict[str, object]
) -> None:
    base = _identity(make_kokoro_speaker(_settings(kokoro_env), FakePlayer()))
    other = make_kokoro_speaker(_settings(kokoro_env, **overrides), FakePlayer())
    assert _identity(other) == base


@pytest.mark.parametrize(
    "overrides",
    [
        {"volume": 80},
        {"rate": 2},
        {"radio_click": False},
        {"tone_urgent_speed": 1.2},
        {"kokoro_lang": "en-gb"},
    ],
)
def test_cache_identity_follows_audio_settings(
    kokoro_env: SimpleNamespace, overrides: dict[str, object]
) -> None:
    base = _identity(make_kokoro_speaker(_settings(kokoro_env), FakePlayer()))
    other = make_kokoro_speaker(_settings(kokoro_env, **overrides), FakePlayer())
    assert _identity(other) != base


def test_cuda_session_uses_bounded_gpu_pool(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    assert len(kokoro_env.sessions) == 1
    assert kokoro_env.sessions[0][1] == [
        (
            "CUDAExecutionProvider",
            {
                "cudnn_conv_algo_search": "DEFAULT",
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_use_max_workspace": "0",
                "gpu_mem_limit": str(GPU_MEM_LIMIT_MB * 1024 * 1024),
            },
        ),
        "CPUExecutionProvider",
    ]
    assert kokoro_env.session_options == [None]
    assert kokoro_env.preloads == [True]
    assert kokoro_env.logger_severities == [3]
    assert speaker.name == "kokoro (af_heart, cuda)"
    speaker.close()


def test_cuda_startup_presizes_gpu_pool_with_long_line(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    assert [c["text"] for c in kokoro_env.creates] == ["Radio check.", PRESIZE_TEXT]
    speaker.close()


def test_cpu_startup_renders_only_radio_check(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env, kokoro_device="cpu"), FakePlayer())
    assert [c["text"] for c in kokoro_env.creates] == ["Radio check."]
    speaker.close()


@pytest.mark.parametrize(("cuda_is_available", "device"), [(True, "cpu"), (False, "auto")])
def test_cpu_session_when_cuda_is_forced_off_or_missing(
    kokoro_env: SimpleNamespace, cuda_is_available: bool, device: str
) -> None:
    kokoro_env.cuda = cuda_is_available
    speaker = make_kokoro_speaker(_settings(kokoro_env, kokoro_device=device), FakePlayer())
    assert kokoro_env.sessions[-1][1] == ["CPUExecutionProvider"]
    assert kokoro_env.session_options[-1].intra_op_num_threads == 4
    assert kokoro_env.session_options[-1].inter_op_num_threads == 1
    assert kokoro_env.preloads == []
    assert speaker.name == "kokoro (af_heart, cpu)"
    speaker.close()


def test_gpu_render_failure_retries_on_cpu_once(kokoro_env: SimpleNamespace) -> None:
    speaker = make_kokoro_speaker(_settings(kokoro_env), FakePlayer())
    wav, seconds = speaker.render("gpu-bad", 2)
    assert assert_pitwall_wav(wav) == pytest.approx(seconds)
    assert len(kokoro_env.sessions) == 2
    assert kokoro_env.sessions[-1][1] == ["CPUExecutionProvider"]
    speaker._renderer.render("gpu-bad", 2)
    assert len(kokoro_env.sessions) == 2
    kokoro_env.creates.clear()
    speaker._renderer.render("normal", 2)
    assert kokoro_env.creates[-1]["device"] == "CUDAExecutionProvider"
    speaker.close()


def test_missing_model_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=KOKORO_MODEL):
        make_kokoro_speaker(SpeechSettings(engine="kokoro", voices_dir=str(tmp_path)))


@pytest.mark.parametrize(
    "files", [(), (KOKORO_MODEL,), (KOKORO_VOICES,), (KOKORO_MODEL, KOKORO_VOICES)]
)
@pytest.mark.parametrize("piper_installed", [False, True])
def test_auto_engine_prefers_complete_kokoro_files(
    tmp_path: Path, files: tuple[str, ...], piper_installed: bool
) -> None:
    settings = SpeechSettings(voices_dir=str(tmp_path))
    directory = tmp_path / "kokoro"
    directory.mkdir()
    for name in files:
        (directory / name).touch()
    if piper_installed:
        voice_path(settings).touch()
    expected = "kokoro" if len(files) == 2 else "piper" if piper_installed else "null"
    assert auto_engine(settings) == expected


@pytest.mark.parametrize("engine", ["kokoro", "auto"])
def test_voice_warm_follows_profile_for_kokoro(
    kokoro_env: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    engine: str,
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        json.dumps({"speech": {"engine": engine, "voices_dir": str(kokoro_env.dir)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))
    monkeypatch.chdir(tmp_path)
    warmed = []
    closed = []

    def factory(settings: SpeechSettings) -> SimpleNamespace:
        assert settings.voices_dir == str(kokoro_env.dir)
        return SimpleNamespace(
            name="kokoro",
            warm=lambda phrases, **_kwargs: warmed.extend(phrases) or len(phrases),
            close=lambda: closed.append(True),
        )

    monkeypatch.setitem(ENGINES, "kokoro", EngineSpec("kokoro", factory, "piper"))
    assert main(["voice", "warm"]) == 0
    captured = capsys.readouterr()
    assert "voice warm: loading kokoro" in captured.err
    assert "phrases for kokoro" in captured.out
    assert warmed
    assert closed == [True]
    assert kokoro_env.sessions == []


def test_auto_missing_kokoro_package_falls_back_to_piper(
    kokoro_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    sentinel = object()
    monkeypatch.setitem(ENGINES, "piper", EngineSpec("piper", lambda _s: sentinel, "null"))
    assert make_speaker(SpeechSettings(engine="auto", voices_dir=str(kokoro_env.dir))) is sentinel


def test_missing_kokoro_package_names_the_fix(
    kokoro_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    with pytest.raises(RuntimeError, match="uv sync --extra kokoro"):
        make_kokoro_speaker(_settings(kokoro_env), FakePlayer())


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
