"""Kokoro-82M neural TTS backend (ONNX, CPU): more natural than Piper, still light.

Synthesis runs on a small, non-spinning onnxruntime thread pool
(`speech.kokoro_threads`, default 2) so it doesn't compete with the game for
CPU. Rendering, caching, preemption and playback reuse `PiperSpeaker`.

Model files live in `speech.voices_dir` (`pitwall voices kokoro`).
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np

from pitwall.audio.piper_tts import PiperSpeaker, Synth, WinsoundPlayer, radio_blip, to_wav
from pitwall.config.models import SpeechSettings

KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
SUGGESTED_KOKORO_VOICES = ("bm_george", "bm_lewis", "bm_daniel", "bf_emma", "am_michael")


def kokoro_paths(settings: SpeechSettings) -> tuple[Path, Path]:
    d = Path(settings.voices_dir)
    return d / KOKORO_MODEL, d / KOKORO_VOICES


def kokoro_installed(settings: SpeechSettings) -> bool:
    return all(p.exists() for p in kokoro_paths(settings))


def download_kokoro(settings: SpeechSettings) -> list[Path]:
    d = Path(settings.voices_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for path in kokoro_paths(settings):
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".part")
            urllib.request.urlretrieve(f"{KOKORO_RELEASE}/{path.name}", tmp)
            tmp.replace(path)
        out.append(path)
    return out


def make_kokoro_tone_synths(settings: SpeechSettings) -> dict[int, Synth]:
    """One synth per call priority sharing one loaded model; P1 quicker, P3 slower."""
    import onnxruntime as ort  # type: ignore[import-untyped]
    from kokoro_onnx import Kokoro

    model, voices = kokoro_paths(settings)
    if not (model.exists() and voices.exists()):
        raise FileNotFoundError(
            f"Kokoro model not found in {settings.voices_dir}/ (run: pitwall voices kokoro)"
        )
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, settings.kokoro_threads)
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
    kokoro = Kokoro.from_session(session, str(voices))
    if settings.kokoro_voice not in kokoro.get_voices():
        raise ValueError(f"Kokoro voice {settings.kokoro_voice!r} not in {voices.name}")
    volume = max(0, min(100, settings.volume)) / 100.0
    base_speed = max(0.5, min(2.0, settings.kokoro_speed))
    tones = {1: settings.tone_urgent_speed, 2: 1.0, 3: settings.tone_calm_speed}
    blips: dict[int, np.ndarray] = {}

    def make(tone: float) -> Synth:
        speed = max(0.5, min(2.0, base_speed * tone))

        def synth(text: str) -> tuple[bytes, float]:
            audio, sample_rate = kokoro.create(
                text, voice=settings.kokoro_voice, speed=speed, lang=settings.kokoro_lang
            )
            if settings.radio_click:
                blip = blips.setdefault(sample_rate, radio_blip(sample_rate))
                audio = np.concatenate([blip, audio])
            samples = audio * volume
            return to_wav(samples, sample_rate), len(samples) / sample_rate

        return synth

    return {prio: make(tone) for prio, tone in tones.items()}


def make_kokoro_speaker(settings: SpeechSettings) -> PiperSpeaker:
    player = WinsoundPlayer()
    tones = make_kokoro_tone_synths(settings)
    return PiperSpeaker(tones[2], player, label=f"kokoro ({settings.kokoro_voice})", tones=tones)
