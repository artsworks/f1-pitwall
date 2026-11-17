"""Kokoro WAV renderer on kokoro-onnx.

The model files live in `speech.voices_dir/kokoro/`. With onnxruntime-gpu the session
runs on CUDA when CUDA is available.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.audio.wav_speaker import Player, WavSpeaker, default_player, radio_blip, to_wav
from pitwall.config.models import SpeechSettings
from pitwall.config.paths import storage_path

KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1"
_ERS = re.compile(r"\bERS\b")


def kokoro_dir(settings: SpeechSettings) -> Path:
    return storage_path(settings.voices_dir) / "kokoro"


def _speed(settings: SpeechSettings, priority: int) -> float:
    rate = max(-10, min(10, settings.rate))
    base = settings.kokoro_speed / (1.0 - 0.04 * rate)
    if priority == 1:
        speed = base * settings.tone_urgent_speed
    elif priority == 3:
        speed = base * settings.tone_calm_speed
    else:
        speed = base
    return max(0.5, min(2.0, speed))


class KokoroRenderer:
    per_priority = True

    def __init__(
        self, kokoro: Any, settings: SpeechSettings, label: str, cache_identity: str
    ) -> None:
        self._kokoro = kokoro
        self._settings = settings
        self.label = label
        self.cache_identity = cache_identity

    def render(self, text: str, priority: int, *, background: bool = False) -> tuple[bytes, float]:
        samples, sample_rate = self._kokoro.create(
            _ERS.sub("E R S", text),
            voice=self._settings.kokoro_voice,
            speed=_speed(self._settings, priority),
            lang=self._settings.kokoro_lang,
        )
        volume = max(0, min(100, self._settings.volume)) / 100.0
        samples = samples * volume
        if self._settings.radio_click:
            samples = np.concatenate((radio_blip(sample_rate), samples))
        return to_wav(samples, sample_rate), len(samples) / sample_rate


def make_kokoro_speaker(settings: SpeechSettings, player: Player | None = None) -> WavSpeaker:
    directory = kokoro_dir(settings)
    model = directory / KOKORO_MODEL
    voices = directory / KOKORO_VOICES
    missing = [path.name for path in (model, voices) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Kokoro file(s) {', '.join(missing)} not found in {directory}/ "
            f"(download from {KOKORO_RELEASE})"
        )

    import onnxruntime  # type: ignore[import-untyped,import-not-found,unused-ignore]

    try:
        from kokoro_onnx import (
            Kokoro,  # type: ignore[import-untyped,import-not-found,unused-ignore]
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "the Kokoro packages are not installed. Run `uv sync --extra kokoro`"
        ) from exc

    providers: list[Any]
    if settings.kokoro_device == "auto" and "CUDAExecutionProvider" in (
        onnxruntime.get_available_providers()
    ):
        if hasattr(onnxruntime, "preload_dlls"):
            onnxruntime.preload_dlls()
        providers = [
            ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}),
            "CPUExecutionProvider",
        ]
    else:
        providers = ["CPUExecutionProvider"]
    onnxruntime.set_default_logger_severity(3)
    session = onnxruntime.InferenceSession(str(model), providers=providers)
    kokoro = Kokoro.from_session(session, str(voices))
    if settings.kokoro_voice not in kokoro.voices:
        raise ValueError(f"Kokoro voice {settings.kokoro_voice!r} is not in {voices.name}")
    device = "cuda" if session.get_providers()[0] == "CUDAExecutionProvider" else "cpu"

    identity = hashlib.sha256(
        json.dumps(
            {
                "engine": "kokoro",
                "settings": settings.model_dump(mode="json"),
                "model_size": model.stat().st_size,
                "model_mtime_ns": model.stat().st_mtime_ns,
                "voices_size": voices.stat().st_size,
                "voices_mtime_ns": voices.stat().st_mtime_ns,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    renderer = KokoroRenderer(
        kokoro,
        settings,
        f"kokoro ({settings.kokoro_voice}, {device})",
        identity,
    )
    renderer.render("Radio check.", 2)
    return WavSpeaker(
        renderer,
        player or default_player(),
        cache_dir=storage_path(settings.voices_dir) / ".phrases",
    )
