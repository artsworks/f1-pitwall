"""Piper neural TTS backend: offline, natural voice, SAPI kept as fallback.

`PiperSpeaker` renders each call to a WAV (optional radio blip + speech) on
its worker thread and hands it to a `Player`; `on_spoken` fires at playback
start. Rendered WAVs are cached by text, so repeated calls skip synthesis.
A P1 call preempts whatever is playing; queued calls play in priority order.

Voices are downloaded once into `speech.voices_dir` (`pitwall voices get`).
"""

from __future__ import annotations

import io
import itertools
import queue
import sys
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from pitwall.audio.dispatcher import Call
from pitwall.config.models import SpeechSettings

SUGGESTED_VOICES = (
    "en_GB-northern_english_male-medium",
    "en_GB-alan-medium",
    "en_US-ryan-high",
    "en_US-lessac-medium",
)
CACHE_SIZE = 64
BLIP_PIPS = ((587.0, 0.06, 0.025), (784.0, 0.07, 0.03))
BLIP_PIP_GAP_S = 0.03
BLIP_GAP_S = 0.06
BLIP_AMP = 0.16
BLIP_H2 = 0.12
BLIP_ATTACK_S = 0.012
BLIP_RELEASE_S = 0.008

Synth = Callable[[str], tuple[bytes, float]]


class Player(Protocol):
    def play(self, wav: bytes) -> None: ...

    def stop(self) -> None: ...


class WinsoundPlayer:
    """Async playback via winsound; SND_ASYNC needs a file, not memory."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WinsoundPlayer requires Windows")
        import winsound  # noqa: F401

        self._dir: Path = Path(tempfile.mkdtemp(prefix="pitwall-tts-"))
        self._slot: int = 0

    def play(self, wav: bytes) -> None:
        import winsound

        self._slot ^= 1
        path = self._dir / f"call{self._slot}.wav"
        path.write_bytes(wav)
        winsound.PlaySound(  # type: ignore[attr-defined]
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,  # type: ignore[attr-defined]
        )

    def stop(self) -> None:
        import winsound

        winsound.PlaySound(None, 0)  # type: ignore[attr-defined]


def voice_path(settings: SpeechSettings, name: str | None = None) -> Path:
    return Path(settings.voices_dir) / f"{name or settings.piper_voice}.onnx"


def installed_voices(settings: SpeechSettings) -> list[str]:
    d = Path(settings.voices_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.onnx") if p.with_suffix(".onnx.json").exists())


def download_voice(settings: SpeechSettings, name: str) -> Path:
    from piper.download_voices import download_voice as _download

    d = Path(settings.voices_dir)
    d.mkdir(parents=True, exist_ok=True)
    _download(name, d)
    return voice_path(settings, name)


def _blip_envelope(sample_count: int, sample_rate: int, tau_s: float) -> np.ndarray:
    t = np.arange(sample_count) / sample_rate
    attack_samples = int(sample_rate * BLIP_ATTACK_S)
    envelope = np.exp(-np.maximum(t - BLIP_ATTACK_S, 0) / tau_s)
    envelope[:attack_samples] *= 0.5 - 0.5 * np.cos(
        np.pi * np.arange(attack_samples) / attack_samples
    )
    release_samples = int(sample_rate * BLIP_RELEASE_S)
    envelope[-release_samples:] *= 0.5 + 0.5 * np.cos(
        np.pi * np.arange(release_samples) / release_samples
    )
    return envelope


def _pip(sample_rate: int, hz: float, seconds: float, tau_s: float) -> np.ndarray:
    sample_count = int(sample_rate * seconds)
    frequencies = np.linspace(hz, hz, sample_count)
    phase = 2 * np.pi * np.cumsum(frequencies) / sample_rate
    tone = np.sin(phase) + BLIP_H2 * np.sin(2 * phase)
    pip: np.ndarray = (
        BLIP_AMP * tone * _blip_envelope(sample_count, sample_rate, tau_s) / (1 + BLIP_H2)
    )
    return pip


def radio_blip(sample_rate: int) -> np.ndarray:
    """Return the soft radio blip followed by its silent gap."""
    pips = [_pip(sample_rate, hz, seconds, tau_s) for hz, seconds, tau_s in BLIP_PIPS]
    pip_gap = np.zeros(int(sample_rate * BLIP_PIP_GAP_S))
    trailing_gap = np.zeros(int(sample_rate * BLIP_GAP_S))
    return np.concatenate([pips[0], pip_gap, pips[1], trailing_gap])


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def make_piper_synth(settings: SpeechSettings) -> Synth:
    """Load the configured Piper voice; return text -> (wav bytes, seconds)."""
    from piper import PiperVoice, SynthesisConfig

    path = voice_path(settings)
    if not path.exists():
        raise FileNotFoundError(
            f"Piper voice {settings.piper_voice} not found in {settings.voices_dir}/ "
            f"(run: pitwall voices get {settings.piper_voice})"
        )
    voice = PiperVoice.load(path)
    rate = max(-10, min(10, settings.rate))
    cfg = SynthesisConfig(
        length_scale=(1.0 - 0.04 * rate) / max(0.5, min(2.0, settings.piper_speed)),
        volume=max(0, min(100, settings.volume)) / 100.0,
    )
    sample_rate = voice.config.sample_rate
    blip = radio_blip(sample_rate) if settings.radio_click else np.zeros(0)

    def synth(text: str) -> tuple[bytes, float]:
        parts = [c.audio_float_array for c in voice.synthesize(text, syn_config=cfg)]
        samples = np.concatenate([blip, *parts]) if parts else blip
        return to_wav(samples, sample_rate), len(samples) / sample_rate

    return synth


class PiperSpeaker:
    name = "piper"

    def __init__(self, synth: Synth, player: Player, label: str = "piper") -> None:
        self.name = label
        self.on_spoken: Callable[[str, float], None] | None = None
        self._synth = synth
        self._player = player
        self._cache: OrderedDict[str, tuple[bytes, float]] = OrderedDict()
        self._q: queue.PriorityQueue[tuple[int, int, Call | None]] = queue.PriorityQueue()
        self._seq = itertools.count()
        self._cancelled: set[str] = set()
        self._urgent = threading.Event()
        self.busy_until = 0.0
        self._thread = threading.Thread(target=self._worker, daemon=True, name="piper")
        self._thread.start()

    def speak(self, call: Call) -> None:
        if call.priority == 1:
            self._urgent.set()
        self._q.put((call.priority, next(self._seq), call))

    def cancel(self, call_id: str) -> None:
        self._cancelled.add(call_id)

    def close(self) -> None:
        self._q.put((-1, next(self._seq), None))
        self._thread.join(timeout=2.0)
        self._player.stop()

    def render(self, text: str) -> tuple[bytes, float]:
        hit = self._cache.get(text)
        if hit is not None:
            self._cache.move_to_end(text)
            return hit
        out = self._synth(text)
        self._cache[text] = out
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return out

    def _worker(self) -> None:
        while True:
            _, _, call = self._q.get()
            if call is None:
                return
            if call.id in self._cancelled:
                self._cancelled.discard(call.id)
                continue
            try:
                wav, seconds = self.render(call.text)
            except Exception as exc:
                print(f"speech: piper synthesis failed: {exc!r}", file=sys.stderr)
                continue
            if call.id in self._cancelled:
                self._cancelled.discard(call.id)
                continue
            if call.priority == 1:
                self._urgent.clear()
            self._player.play(wav)
            self.busy_until = time.monotonic() + seconds
            if self.on_spoken is not None:
                self.on_spoken(call.id, time.time())
            if self._urgent.wait(seconds):
                self._player.stop()


def make_piper_speaker(settings: SpeechSettings) -> PiperSpeaker:
    player = WinsoundPlayer()
    synth = make_piper_synth(settings)
    speaker = PiperSpeaker(synth, player, label=f"piper ({settings.piper_voice})")
    return speaker
