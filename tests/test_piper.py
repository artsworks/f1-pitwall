from __future__ import annotations

import io
import os
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from pitwall.audio.piper_tts import (
    PiperRenderer,
    common_phrases,
    make_piper_synth,
    radio_blip,
    to_wav,
)
from pitwall.audio.speaker import (
    REJOIN_OPENER,
    STARTUP_LINES,
    STARTUP_PRIORITY,
    NullSpeaker,
    make_speaker,
)
from pitwall.audio.wav_speaker import WarmProcess, WavSpeaker, default_warm_workers
from pitwall.config.models import SpeechSettings
from tests.audio_contract import FakePlayer


def test_prerendered_phrases_survive_restart_and_keep_tone_separate(
    tmp_path: Path,
) -> None:
    rendered: list[str] = []
    wav = to_wav(np.zeros(160), 16_000)

    def synth(text: str) -> tuple[bytes, float]:
        rendered.append(text)
        return wav, 0.01

    renderer = PiperRenderer(
        {1: synth, 2: synth},
        synth,
        "piper-test",
        "configured-voice-tone",
    )
    first = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    assert first.warm([("Box this lap", 1), ("Box this lap", 2)]) == 2
    assert rendered == ["Box this lap", "Box this lap"]
    first.close()

    restarted = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    assert restarted.render("Box this lap", 1) == (wav, 0.01)
    assert restarted.render("Box this lap", 2) == (wav, 0.01)
    assert len(rendered) == 2
    restarted.close()
    assert all("{" not in text for text, _ in common_phrases())


def test_common_phrases_limit() -> None:
    every = common_phrases(10_000)
    assert common_phrases(2) == [(text, STARTUP_PRIORITY) for _, text in STARTUP_LINES]
    assert (REJOIN_OPENER, STARTUP_PRIORITY) in every
    assert ("About a lap left in the tyres", 3) in every
    assert ("Another hit. Checking the car again", 1) in every
    assert ("Copy. Go get it", 1) in every
    assert ("extra phrase", 1) in common_phrases(10_000, [("extra phrase", 1)])
    assert all("{" not in text for text, _ in every)
    assert len(every) > 150
    assert common_phrases() == every[:150]
    assert len(common_phrases(10)) == 10


def test_to_wav_roundtrip() -> None:
    data = to_wav(np.zeros(2205), 22050)
    with wave.open(io.BytesIO(data)) as reader:
        assert (
            reader.getframerate(),
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getnframes(),
        ) == (22050, 1, 2, 2205)


def test_radio_blip_shape() -> None:
    sample_rate = 22050
    pip1_samples = int(sample_rate * 0.06)
    pip_gap_samples = int(sample_rate * 0.03)
    pip2_samples = int(sample_rate * 0.07)
    trailing_gap_samples = int(sample_rate * 0.06)
    pip2_start = pip1_samples + pip_gap_samples
    trailing_gap_start = pip2_start + pip2_samples
    blip = radio_blip(sample_rate)

    assert len(blip) == pip1_samples + pip_gap_samples + pip2_samples + trailing_gap_samples
    assert 0.1 < np.abs(blip).max() < 0.2
    assert abs(blip[0]) < 1e-3
    assert abs(blip[pip1_samples - 1]) < 1e-3
    assert abs(blip[pip2_start]) < 1e-3
    assert abs(blip[trailing_gap_start - 1]) < 1e-3
    assert np.all(blip[pip1_samples:pip2_start] == 0)
    assert np.all(blip[trailing_gap_start:] == 0)

    magnitude = np.abs(np.fft.rfft(blip))
    frequencies = np.fft.rfftfreq(len(blip), 1 / sample_rate)
    assert np.sum(magnitude * frequencies) / np.sum(magnitude) < 1000


def test_missing_voice_falls_back(tmp_path: Path) -> None:
    settings = SpeechSettings(engine="piper", voices_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="pitwall voice get"):
        make_piper_synth(settings)
    assert isinstance(make_speaker(settings), NullSpeaker)


def test_speech_disabled_makes_null_speaker() -> None:
    assert isinstance(make_speaker(SpeechSettings(enabled=False, engine="piper")), NullSpeaker)


VOICES = Path.home() / "voices"


@pytest.mark.skipif(not (VOICES / "en_GB-alan-medium.onnx").exists(), reason="no voice")
def test_real_piper_synth_renders_speech() -> None:
    synth = make_piper_synth(SpeechSettings(voices_dir=str(VOICES)))
    wav, seconds = synth("Box this lap.")
    assert 0.5 < seconds < 5.0
    with wave.open(io.BytesIO(wav)) as reader:
        assert reader.getnframes() / reader.getframerate() == pytest.approx(seconds)


def test_piper_renderer_uses_priority_specific_tones() -> None:
    used: list[str] = []

    def tone(name: str) -> Callable[[str], tuple[bytes, float]]:
        def synth(text: str) -> tuple[bytes, float]:
            used.append(name)
            return f"{name}:{text}".encode(), 0.0

        return synth

    tones = {1: tone("urgent"), 2: tone("normal"), 3: tone("calm")}
    renderer = PiperRenderer(tones, tones[2], "piper-test", "test-key")
    assert renderer.render("x", 3)[0] == b"calm:x"
    assert renderer.render("x", 1)[0] == b"urgent:x"
    assert used == ["calm", "urgent"]


def test_piper_renderer_warm_process_attributes() -> None:
    def synth(text: str) -> tuple[bytes, float]:
        return b"wav", 0.0

    renderer = PiperRenderer({2: synth}, synth, "piper-test", "key")
    assert renderer.warm_parallelism is None
    assert renderer.warm_workers == default_warm_workers()
    assert renderer.warm_process is None

    spec = WarmProcess(lambda _settings: None, (), lambda _task: (b"wav", 0.0))
    configured = PiperRenderer(
        {2: synth},
        synth,
        "piper-test",
        "key",
        warm_workers=4,
        warm_process=spec,
    )
    assert configured.warm_parallelism == "process"
    assert configured.warm_workers == 4
    assert configured.warm_process is spec


def test_piper_warm_render_uses_loaded_tones(monkeypatch: pytest.MonkeyPatch) -> None:
    import pitwall.audio.piper_tts as piper_module

    wav = to_wav(np.zeros(160), 16_000)
    tones = {
        1: lambda _text: (wav, 0.01),
        2: lambda _text: (wav, 0.02),
        3: lambda _text: (wav, 0.03),
    }
    monkeypatch.setattr(piper_module, "_WARM_TONES", tones)

    assert piper_module._warm_render(("line", 1)) == (wav, 0.01)
    assert piper_module._warm_render(("line", 3)) == (wav, 0.03)
    assert piper_module._warm_render(("line", 9)) == (wav, 0.02)


def test_default_warm_workers_halves_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert default_warm_workers() == 8
    assert default_warm_workers(cap=4) == 4
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert default_warm_workers() == 1
    assert default_warm_workers(cap=4) == 1


def test_piper_warm_init_loads_single_threaded_tones(monkeypatch: pytest.MonkeyPatch) -> None:
    import pitwall.audio.piper_tts as piper_module

    monkeypatch.setattr(piper_module, "_WARM_TONES", None)
    recorded: dict[str, int | None] = {}

    def fake_make(
        settings: SpeechSettings, *, ort_threads: int | None = None
    ) -> dict[int, Callable[[str], tuple[bytes, float]]]:
        recorded["ort_threads"] = ort_threads
        return {2: lambda _text: (b"wav", 0.0)}

    monkeypatch.setattr(piper_module, "make_piper_tone_synths", fake_make)
    piper_module._warm_init(SpeechSettings())
    assert recorded["ort_threads"] == 1
    assert piper_module._WARM_TONES == {2: piper_module._WARM_TONES[2]}
