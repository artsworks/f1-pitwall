from __future__ import annotations

import hashlib
import io
import itertools
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import wave
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import numpy as np

from pitwall.audio.dispatcher import Call

if TYPE_CHECKING:
    from pitwall.audio.speaker import Speaker

log = logging.getLogger(__name__)

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


class WavRenderer(Protocol):
    label: str
    cache_identity: str
    per_priority: bool

    def render(
        self, text: str, priority: int, *, background: bool = False
    ) -> tuple[bytes, float]: ...


class InvalidWav(ValueError):
    pass


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

        windows_audio = cast(Any, winsound)
        self._slot ^= 1
        path = self._dir / f"call{self._slot}.wav"
        path.write_bytes(wav)
        windows_audio.PlaySound(
            str(path),
            windows_audio.SND_FILENAME | windows_audio.SND_ASYNC | windows_audio.SND_NODEFAULT,
        )

    def stop(self) -> None:
        import winsound

        cast(Any, winsound).PlaySound(None, 0)


class NullPlayer:
    def play(self, wav: bytes) -> None:
        pass

    def stop(self) -> None:
        pass


def default_player() -> Player:
    return WinsoundPlayer() if sys.platform == "win32" else NullPlayer()


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
    phase = 2 * np.pi * hz * np.arange(1, sample_count + 1) / sample_rate
    tone = np.sin(phase) + BLIP_H2 * np.sin(2 * phase)
    pip: np.ndarray = (
        BLIP_AMP * tone * _blip_envelope(sample_count, sample_rate, tau_s) / (1 + BLIP_H2)
    )
    return pip


def radio_blip(sample_rate: int) -> np.ndarray:
    """Return the soft radio blip followed by its silent gap."""
    pips = [_pip(sample_rate, hz, seconds, tau_s) for hz, seconds, tau_s in BLIP_PIPS]
    pip_gap = np.zeros(int(sample_rate * BLIP_PIP_GAP_S))
    parts: list[np.ndarray] = []
    for pip in pips:
        parts += [pip, pip_gap]
    parts[-1] = np.zeros(int(sample_rate * BLIP_GAP_S))
    return np.concatenate(parts)


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.tobytes())
    return buf.getvalue()


def wav_info(wav: bytes) -> float:
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise InvalidWav("expected RIFF/WAVE data")
    try:
        with wave.open(io.BytesIO(wav), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frames = reader.getnframes()
            if channels != 1:
                raise InvalidWav("WAV must have one channel")
            if sample_width != 2:
                raise InvalidWav("WAV must use 16-bit samples")
            if sample_rate <= 0:
                raise InvalidWav("WAV sample rate must be positive")
            if frames <= 0:
                raise InvalidWav("WAV must contain audio frames")
            if len(reader.readframes(frames)) != frames * channels * sample_width:
                raise InvalidWav("WAV frame data is truncated")
            return frames / sample_rate
    except (wave.Error, EOFError) as exc:
        raise InvalidWav(f"invalid WAV data: {exc}") from exc


def pcm_to_wav(
    chunks: Iterable[bytes | np.ndarray],
    sample_rate: int,
    *,
    channels: int = 1,
    dtype: str = "int16",
) -> tuple[bytes, float]:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if channels <= 0:
        raise ValueError("channels must be positive")
    if dtype not in ("int16", "float32"):
        raise ValueError("dtype must be 'int16' or 'float32'")
    pcm_type = np.dtype("<i2" if dtype == "int16" else "<f4")
    parts: list[np.ndarray] = []
    for chunk in chunks:
        if isinstance(chunk, bytes):
            samples = np.frombuffer(chunk, dtype=pcm_type)
        else:
            samples = np.asarray(chunk, dtype=pcm_type)
        parts.append(samples.reshape(-1))
    interleaved = np.concatenate(parts) if parts else np.zeros(0, dtype=pcm_type)
    if len(interleaved) % channels:
        raise ValueError("PCM sample count must be divisible by channels")
    samples_f32 = (
        interleaved.astype(np.float32) / 32768.0
        if dtype == "int16"
        else interleaved.astype(np.float32)
    )
    mono = samples_f32.reshape(-1, channels).mean(axis=1) if channels > 1 else samples_f32
    wav = to_wav(mono, sample_rate)
    return wav, len(mono) / sample_rate


def _write_cache(path: Path, wav: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        Path(temporary).write_bytes(wav)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_cache(path: Path) -> tuple[bytes, float] | None:
    try:
        wav = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        seconds = wav_info(wav)
    except InvalidWav as exc:
        log.warning("Invalid phrase cache %s: %s", path, exc)
        path.unlink(missing_ok=True)
        return None
    return wav, seconds


class WavSpeaker:
    speaks_audio = True
    supports_audio_stream = True

    def __init__(
        self,
        renderer: WavRenderer,
        player: Player,
        *,
        cache_dir: Path | None = None,
        fallback: Speaker | None = None,
    ) -> None:
        self.name = renderer.label
        self.on_spoken: Callable[[str, float], None] | None = None
        self.on_audio: Callable[[str, int, bytes], None] | None = None
        self.on_dropped: Callable[[str], None] | None = None
        self.busy_until = 0.0
        self._renderer = renderer
        self._player = player
        self._cache_dir = cache_dir
        self._fallback = fallback
        self._cache: OrderedDict[tuple[int, str], tuple[bytes, float]] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._q: queue.PriorityQueue[tuple[int, int, Call | None]] = queue.PriorityQueue()
        self._received: dict[str, float] = {}
        self._seq = itertools.count()
        self._cancelled: set[str] = set()
        self._gen = 0
        self._urgent = threading.Event()
        self._background_queue: queue.Queue[tuple[tuple[int, str], str, int] | None] = queue.Queue()
        self._pending: set[tuple[int, str]] = set()
        self._has_provisional = callable(getattr(renderer, "provisional", None))
        self._thread = threading.Thread(target=self._worker, daemon=True, name="wav-speaker")
        self._thread.start()
        self._background_thread: threading.Thread | None = None
        if self._has_provisional:
            self._background_thread = threading.Thread(
                target=self._background_worker, daemon=True, name="wav-renderer"
            )
            self._background_thread.start()

    def _key(self, text: str, priority: int) -> tuple[int, str]:
        return (priority if self._renderer.per_priority else 0, text)

    def _phrase_path(self, key: tuple[int, str]) -> Path | None:
        if self._cache_dir is None:
            return None
        digest = hashlib.sha256(
            json.dumps((self._renderer.cache_identity, key)).encode()
        ).hexdigest()
        return self._cache_dir / f"{digest}.wav"

    def _remember(self, key: tuple[int, str], result: tuple[bytes, float], *, disk: bool) -> None:
        wav, _ = result
        seconds = wav_info(wav)
        result = wav, seconds
        with self._cache_lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        path = self._phrase_path(key)
        if disk and path is not None:
            _write_cache(path, wav)

    def _cached(self, key: tuple[int, str]) -> tuple[bytes, float] | None:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            path = self._phrase_path(key)
            result = _read_cache(path) if path is not None else None
            if result is not None:
                self._cache[key] = result
                self._cache.move_to_end(key)
                if len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
            return result

    def render(self, text: str, priority: int = 2) -> tuple[bytes, float]:
        key = self._key(text, priority)
        hit = self._cached(key)
        if hit is not None:
            return hit
        result = self._renderer.render(text, priority, background=False)
        seconds = wav_info(result[0])
        result = result[0], seconds
        self._remember(key, result, disk=True)
        return result

    def warm(self, phrases: list[tuple[str, int]]) -> int:
        unique = {self._key(text, priority): (text, priority) for text, priority in phrases}
        for key, (text, priority) in unique.items():
            if self._cached(key) is not None:
                continue
            wav, _ = self._renderer.render(text, priority, background=True)
            self._remember(key, (wav, wav_info(wav)), disk=True)
        return len(unique)

    def queue_status(self) -> tuple[int, float]:
        now = time.monotonic()
        with self._state_lock:
            times = list(self._received.values())
        oldest = max((now - received for received in times), default=0.0)
        return len(times), oldest

    def speak(self, call: Call) -> None:
        with self._state_lock:
            if call.priority == 1:
                self._urgent.set()
            self._received[call.id] = time.monotonic()
            self._q.put((call.priority, next(self._seq), call))

    def cancel(self, call_id: str) -> None:
        with self._state_lock:
            self._cancelled.add(call_id)

    def _current(self, generation: int) -> bool:
        return generation == self._gen

    def _get_for_call(self, call: Call) -> tuple[bytes, float]:
        key = self._key(call.text, call.priority)
        hit = self._cached(key)
        if hit is not None:
            return hit
        provisional = getattr(self._renderer, "provisional", None)
        if callable(provisional):
            result = provisional(call.text, call.priority)
            if result is not None:
                seconds = wav_info(result[0])
                self._queue_background(key, call.text, call.priority)
                return result[0], seconds
        return self.render(call.text, call.priority)

    def _queue_background(self, key: tuple[int, str], text: str, priority: int) -> None:
        with self._cache_lock:
            if key not in self._pending:
                self._pending.add(key)
                self._background_queue.put((key, text, priority))

    def _background_worker(self) -> None:
        while True:
            item = self._background_queue.get()
            if item is None:
                return
            key, text, priority = item
            try:
                if self._cached(key) is None:
                    wav, _ = self._renderer.render(text, priority, background=True)
                    self._remember(key, (wav, wav_info(wav)), disk=True)
            except Exception as exc:
                log.warning("%s background synthesis failed for %r: %s", self.name, text, exc)
            finally:
                with self._cache_lock:
                    self._pending.discard(key)

    def _failed(self, call: Call, generation: int, exc: Exception) -> None:
        with self._state_lock:
            if not self._current(generation):
                return
            fallback = self._fallback
            if fallback is not None:
                log.warning(
                    "%s synthesis failed; using %s as fallback: %s",
                    self.name,
                    getattr(fallback, "name", type(fallback).__name__),
                    exc,
                )
                outcome_lock = threading.Lock()
                settled = False

                def on_fallback_spoken(call_id: str, spoken_at: float) -> None:
                    nonlocal settled
                    with outcome_lock:
                        if settled or call_id != call.id:
                            return
                        settled = True
                    with self._state_lock:
                        if not self._current(generation):
                            return
                        callback = self.on_spoken
                    if callback is not None:
                        try:
                            callback(call_id, spoken_at)
                        except Exception:
                            log.exception("%s fallback spoken callback failed", self.name)

                def on_fallback_dropped(call_id: str) -> None:
                    nonlocal settled
                    with outcome_lock:
                        if settled or call_id != call.id:
                            return
                        settled = True
                    self._drop(call_id, generation)

                fallback.on_spoken = on_fallback_spoken
                fallback_callbacks = cast(Any, fallback)
                if hasattr(fallback_callbacks, "on_dropped"):
                    fallback_callbacks.on_dropped = on_fallback_dropped
                try:
                    fallback.speak(call)
                except Exception as fallback_exc:
                    log.warning("%s fallback failed: %s", self.name, fallback_exc)
                    on_fallback_dropped(call.id)
                return
            log.warning("%s synthesis failed: %s", self.name, exc)
            self._drop(call.id, generation)

    def _drop(self, call_id: str, generation: int) -> None:
        with self._state_lock:
            if not self._current(generation) or call_id in self._cancelled:
                return
            callback = self.on_dropped
            if callback is not None:
                try:
                    callback(call_id)
                except Exception:
                    log.exception("%s dropped callback failed", self.name)

    def _play(self, call: Call, wav: bytes, seconds: float, generation: int) -> None:
        with self._state_lock:
            if not self._current(generation) or call.id in self._cancelled:
                return
            try:
                self._player.play(wav)
            except Exception as exc:
                log.warning("%s playback failed: %s", self.name, exc)
                self._drop(call.id, generation)
                return
            self.busy_until = time.monotonic() + seconds
        with self._state_lock:
            if (
                self._current(generation)
                and call.id not in self._cancelled
                and self.on_audio is not None
            ):
                try:
                    self.on_audio(call.id, call.priority, wav)
                except Exception:
                    log.exception("%s audio stream failed", self.name)
        with self._state_lock:
            if self._current(generation) and self.on_spoken is not None:
                try:
                    self.on_spoken(call.id, time.time())
                except Exception:
                    log.exception("%s spoken callback failed", self.name)

    def _worker(self) -> None:
        while True:
            _, _, call = self._q.get()
            if call is None:
                return
            with self._state_lock:
                generation = self._gen
                received = self._received.pop(call.id, time.monotonic())
                cancelled = call.id in self._cancelled
                if cancelled:
                    self._cancelled.discard(call.id)
            if cancelled:
                continue
            try:
                wav, seconds = self._get_for_call(call)
            except Exception as exc:
                self._failed(call, generation, exc)
                continue
            with self._state_lock:
                if not self._current(generation) or call.id in self._cancelled:
                    self._cancelled.discard(call.id)
                    continue
                if (
                    call.max_age_s is not None
                    and call.handoff_age_s + (time.monotonic() - received) > call.max_age_s
                ):
                    drop = True
                else:
                    drop = False
                if call.priority == 1:
                    self._urgent.clear()
            if drop:
                self._drop(call.id, generation)
                continue
            self._play(call, wav, seconds, generation)
            if self._urgent.wait(seconds):
                self._player.stop()

    def flush(self) -> None:
        with self._state_lock:
            self._gen += 1
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._cancelled.clear()
            self._received.clear()
            self._urgent.clear()
            self._player.stop()

    def close(self) -> None:
        self._q.put((-1, next(self._seq), None))
        self._thread.join(timeout=2.0)
        if self._background_thread is not None:
            self._background_queue.put(None)
            self._background_thread.join(timeout=2.0)
        self._player.stop()
        if self._fallback is not None:
            self._fallback.close()
