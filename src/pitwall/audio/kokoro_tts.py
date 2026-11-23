"""Kokoro WAV renderer on kokoro-onnx.

The model files live in `speech.voices_dir/kokoro/`. With onnxruntime-gpu the session
runs on CUDA when CUDA is available.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.audio.piper_tts import lazy_piper_tones
from pitwall.audio.wav_speaker import (
    Player,
    WarmProcess,
    WavSpeaker,
    default_player,
    default_warm_workers,
    radio_blip,
    to_wav,
)
from pitwall.config.models import SpeechSettings
from pitwall.config.paths import storage_path

KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1"
GPU_MEM_LIMIT_MB = 1024
GPU_FAILURES_MAX = 3
WARM_MIN_BATCH = 128
CPU_THREADS = 4
PRESIZE_TEXT = (
    "Box at the end of this lap for hards, we go to the end, expect to come out in clean air "
    "with a four second gap behind."
)
_ERS = re.compile(r"\bERS\b")
log = logging.getLogger(__name__)


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
        self,
        kokoro: Any,
        settings: SpeechSettings,
        label: str,
        cache_identity: str,
        cpu_factory: Callable[[], Any] | None = None,
        provisional_source: Callable[[str, int], tuple[bytes, float] | None] | None = None,
        *,
        warm_workers: int | None = None,
        warm_process: WarmProcess | None = None,
    ) -> None:
        self._kokoro = kokoro
        self._settings = settings
        self.label = label
        self.cache_identity = cache_identity
        self._cpu_factory = cpu_factory
        self._cpu_kokoro: Any = None
        self._cpu_lock = threading.Lock()
        self._provisional_source = provisional_source
        self._gpu_failures = 0
        self.warm_parallelism = "process" if warm_process is not None else None
        self.warm_workers = (
            warm_workers if warm_workers is not None else default_warm_workers(cap=4)
        )
        self.warm_process = warm_process

    def provisional(self, text: str, priority: int) -> tuple[bytes, float] | None:
        if self._provisional_source is None:
            return None
        return self._provisional_source(text, priority)

    def _create(self, text: str, options: dict[str, Any]) -> tuple[Any, int]:
        cpu_factory = self._cpu_factory
        if cpu_factory is not None and self._gpu_failures >= GPU_FAILURES_MAX:
            return self._cpu_create(cpu_factory, text, options)
        try:
            samples, sample_rate = self._kokoro.create(text, **options)
        except Exception as exc:
            if cpu_factory is None:
                raise
            self._gpu_failures += 1
            if self._gpu_failures == GPU_FAILURES_MAX:
                log.warning(
                    "Kokoro GPU failed %d renders in a row; staying on the CPU", GPU_FAILURES_MAX
                )
            log.warning("Kokoro GPU render failed for %r; rendering on the CPU: %s", text, exc)
            return self._cpu_create(cpu_factory, text, options)
        self._gpu_failures = 0
        return samples, sample_rate

    def _cpu_create(
        self, cpu_factory: Callable[[], Any], text: str, options: dict[str, Any]
    ) -> tuple[Any, int]:
        with self._cpu_lock:
            if self._cpu_kokoro is None:
                self._cpu_kokoro = cpu_factory()
            cpu = self._cpu_kokoro
        samples, sample_rate = cpu.create(text, **options)
        return samples, sample_rate

    def render(self, text: str, priority: int, *, background: bool = False) -> tuple[bytes, float]:
        spoken = _ERS.sub("E R S", text)
        options = {
            "voice": self._settings.kokoro_voice,
            "speed": _speed(self._settings, priority),
            "lang": self._settings.kokoro_lang,
        }
        samples, sample_rate = self._create(spoken, options)
        volume = max(0, min(100, self._settings.volume)) / 100.0
        samples = samples * volume
        if self._settings.radio_click:
            samples = np.concatenate((radio_blip(sample_rate), samples))
        return to_wav(samples, sample_rate), len(samples) / sample_rate


_WARM_RENDERER: KokoroRenderer | None = None


def _warm_init(settings: SpeechSettings) -> None:
    """ProcessPoolExecutor initializer: load one Kokoro session per worker."""
    global _WARM_RENDERER
    _WARM_RENDERER = _build_renderer(settings, warm=False)


def _warm_render(task: tuple[str, int]) -> tuple[bytes, float]:
    text, priority = task
    renderer = _WARM_RENDERER
    if renderer is None:
        raise RuntimeError("kokoro warm worker has no loaded session")
    return renderer.render(text, priority, background=True)


def _build_renderer(settings: SpeechSettings, *, warm: bool = True) -> KokoroRenderer:
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
        from kokoro_onnx import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
            Kokoro,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "the Kokoro packages are not installed. Run `uv sync --extra kokoro`"
        ) from exc

    def build_cpu_kokoro() -> Any:
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = CPU_THREADS
        options.inter_op_num_threads = 1
        session = onnxruntime.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        return Kokoro.from_session(session, str(voices))

    use_gpu = settings.kokoro_device == "auto" and "CUDAExecutionProvider" in (
        onnxruntime.get_available_providers()
    )
    if use_gpu and hasattr(onnxruntime, "preload_dlls"):
        onnxruntime.preload_dlls()
    onnxruntime.set_default_logger_severity(3)
    logging.getLogger("phonemizer").setLevel(logging.ERROR)
    if use_gpu:
        providers: list[Any] = [
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
        session = onnxruntime.InferenceSession(str(model), providers=providers)
        kokoro = Kokoro.from_session(session, str(voices))
    else:
        kokoro = build_cpu_kokoro()
    if settings.kokoro_voice not in kokoro.voices:
        raise ValueError(f"Kokoro voice {settings.kokoro_voice!r} is not in {voices.name}")
    on_gpu = use_gpu and session.get_providers()[0] == "CUDAExecutionProvider"
    device = "cuda" if on_gpu else "cpu"

    identity = hashlib.sha256(
        json.dumps(
            {
                "engine": "kokoro",
                "kokoro_voice": settings.kokoro_voice,
                "kokoro_lang": settings.kokoro_lang,
                "kokoro_speed": settings.kokoro_speed,
                "rate": settings.rate,
                "volume": settings.volume,
                "tone_urgent_speed": settings.tone_urgent_speed,
                "tone_calm_speed": settings.tone_calm_speed,
                "radio_click": settings.radio_click,
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
        cpu_factory=build_cpu_kokoro if on_gpu else None,
        provisional_source=lazy_piper_tones(settings),
        warm_workers=settings.warm_workers,
        warm_process=(
            WarmProcess(_warm_init, (settings,), _warm_render, min_batch=WARM_MIN_BATCH)
            if on_gpu and warm
            else None
        ),
    )
    renderer.render("Radio check.", 2)
    if on_gpu:
        renderer.render(PRESIZE_TEXT, 2)
    return renderer


def make_kokoro_speaker(settings: SpeechSettings, player: Player | None = None) -> WavSpeaker:
    renderer = _build_renderer(settings)
    return WavSpeaker(
        renderer,
        player or default_player(),
        cache_dir=storage_path(settings.voices_dir) / ".phrases",
        render_timeout_s=settings.render_timeout_s,
    )
