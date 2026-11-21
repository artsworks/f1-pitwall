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
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import numpy as np

from pitwall.audio.dispatcher import Call

if TYPE_CHECKING:
    from pitwall.audio.speaker import Speaker

log = logging.getLogger(__name__)

CACHE_SIZE = 64
BREAKER_FAILURES = 3
BREAKER_OPEN_S = 60.0
MAX_STUCK_RENDERS = 3
BACKGROUND_MAX = 32
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


@dataclass(frozen=True)
class WarmProcess:
    """Child-process worker for WavSpeaker.warm: the initializer runs once per
    spawned process and render maps (text, priority) to (wav, seconds) there.
    Both must be module-level functions so spawn can pickle them. Smaller
    batches render in-process because starting workers costs more."""

    initializer: Callable[..., None]
    initargs: tuple[Any, ...]
    render: Callable[[tuple[str, int]], tuple[bytes, float]]
    min_batch: int = 16


class InvalidWav(ValueError):
    pass


class RenderTimeout(RuntimeError):
    pass


class RenderSkipped(RuntimeError):
    pass


class _RenderLane[T]:
    """One daemon thread that runs primary renders, so a stuck render can be abandoned."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[Callable[[], T], Future[T]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="wav-render-lane")
        self._thread.start()

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        future: Future[T] = Future()
        self._jobs.put((fn, future))
        return future

    def close(self) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, future = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except Exception as exc:  # noqa: BLE001 - hand the render error to the waiting call
                future.set_exception(exc)


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


class NullPlayer:
    def play(self, wav: bytes) -> None:
        pass

    def stop(self) -> None:
        pass


def default_player() -> Player:
    return WinsoundPlayer() if sys.platform == "win32" else NullPlayer()


def default_warm_workers(cap: int | None = None) -> int:
    """Half the logical cores, so warming leaves CPU for the game and Windows."""
    half = max(1, (os.cpu_count() or 2) // 2)
    return half if cap is None else min(cap, half)


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
    except BaseException:  # noqa: BLE001 - remove the temporary file before propagating errors
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
        render_timeout_s: float | None = None,
    ) -> None:
        self.name = renderer.label
        self.on_spoken: Callable[[str, float], None] | None = None
        self.on_audio: Callable[[str, int, bytes], None] | None = None
        self.on_dropped: Callable[[str], None] | None = None
        self.busy_until = 0.0
        self.render_failures = 0
        self.render_timeouts = 0
        self.provisional_plays = 0
        self.last_error: str | None = None
        self._last_error_t: float | None = None
        self._dropped_count = 0
        self._consecutive_failures = 0
        self._breaker_open_until: float | None = None
        self._renderer = renderer
        self._player = player
        self._gain = 1.0
        self._cache_dir = cache_dir
        self._fallback = fallback
        self._render_timeout_s = (
            render_timeout_s if render_timeout_s is not None and render_timeout_s > 0 else None
        )
        self._lane: _RenderLane[tuple[bytes, float]] | None = (
            _RenderLane() if self._render_timeout_s is not None else None
        )
        self._provisional_lane: _RenderLane[tuple[bytes, float] | None] | None = (
            _RenderLane() if self._render_timeout_s is not None else None
        )
        self._provisional_job: Future[tuple[bytes, float] | None] | None = None
        self._cache: OrderedDict[tuple[int, str], tuple[bytes, float]] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._q: queue.PriorityQueue[tuple[int, int, Call | None]] = queue.PriorityQueue()
        self._received: dict[str, float] = {}
        self._fallback_calls: dict[str, int] = {}
        self._seq = itertools.count()
        self._cancelled: set[str] = set()
        self._gen = 0
        self._urgent = threading.Event()
        self._wake = threading.Event()
        self._background_queue: queue.Queue[tuple[tuple[int, str], str, int] | None] = queue.Queue()
        self._pending: set[tuple[int, str]] = set()
        self._late: set[tuple[int, str]] = set()
        if fallback is not None:
            fallback.on_spoken = self._fallback_spoken
            fallback_callbacks = cast(Any, fallback)
            if hasattr(fallback_callbacks, "on_dropped"):
                fallback_callbacks.on_dropped = self._fallback_dropped
        self._thread = threading.Thread(target=self._worker, daemon=True, name="wav-speaker")
        self._thread.start()
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

    def _remember(
        self,
        key: tuple[int, str],
        result: tuple[bytes, float],
        *,
        disk: bool,
        disk_required: bool = True,
    ) -> None:
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
            try:
                _write_cache(path, wav)
            except OSError as exc:
                if disk_required:
                    raise
                log.warning("%s phrase cache write failed for %s: %s", self.name, path, exc)

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
        self._remember(key, result, disk=True, disk_required=False)
        return result

    def warm(
        self,
        phrases: list[tuple[str, int]],
        *,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> int:
        unique = {self._key(text, priority): (text, priority) for text, priority in phrases}
        todo = [
            (key, text, priority)
            for key, (text, priority) in unique.items()
            if self._cached(key) is None
        ]
        total = len(todo)
        cached = len(unique) - total
        if progress is not None:
            progress(0, total, cached)
        done = 0

        def on_done() -> None:
            nonlocal done
            done += 1
            if progress is not None:
                progress(done, total, cached)

        self._warm_missing(todo, on_done if progress is not None else None)
        return len(unique)

    def _warm_missing(
        self,
        todo: list[tuple[tuple[int, str], str, int]],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        workers = max(1, int(getattr(self._renderer, "warm_workers", 1) or 1))
        if workers <= 1 or len(todo) <= 1:
            self._warm_sequential(todo, on_done)
            return
        mode = getattr(self._renderer, "warm_parallelism", None)
        if mode == "thread":
            with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
                self._warm_collect(
                    todo,
                    lambda text, priority: pool.submit(
                        self._renderer.render, text, priority, background=True
                    ),
                    on_done,
                )
        elif mode == "process":
            spec = getattr(self._renderer, "warm_process", None)
            if spec is None or len(todo) < spec.min_batch:
                self._warm_sequential(todo, on_done)
                return
            with ProcessPoolExecutor(
                max_workers=min(workers, len(todo)),
                initializer=spec.initializer,
                initargs=spec.initargs,
            ) as pool:
                self._warm_collect(
                    todo,
                    lambda text, priority: pool.submit(spec.render, (text, priority)),
                    on_done,
                )
        else:
            self._warm_sequential(todo, on_done)

    def _warm_sequential(
        self,
        todo: list[tuple[tuple[int, str], str, int]],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        for key, text, priority in todo:
            wav, _ = self._renderer.render(text, priority, background=True)
            self._remember(key, (wav, wav_info(wav)), disk=True)
            if on_done is not None:
                on_done()

    def _warm_collect(
        self,
        todo: list[tuple[tuple[int, str], str, int]],
        submit: Callable[[str, int], Future[tuple[bytes, float]]],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        futures = {submit(text, priority): key for key, text, priority in todo}
        error: BaseException | None = None
        for future in as_completed(futures):
            if future.cancelled():
                continue
            exc = future.exception()
            if exc is not None:
                if error is None:
                    error = exc
                    for pending in futures:
                        pending.cancel()
                continue
            wav, _ = future.result()
            self._remember(futures[future], (wav, wav_info(wav)), disk=True)
            if on_done is not None:
                on_done()
        if error is not None:
            raise error

    def health(self) -> dict[str, Any]:
        last_error_t = self._last_error_t
        return {
            "engine": self.name,
            "worker_alive": self._thread.is_alive(),
            "breaker_open": self._breaker_open(),
            "render_failures": self.render_failures,
            "render_timeouts": self.render_timeouts,
            "provisional_plays": self.provisional_plays,
            "dropped": self._dropped_count,
            "last_error": self.last_error,
            "last_error_age_s": None if last_error_t is None else time.monotonic() - last_error_t,
        }

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
                self._wake.set()
            self._received[call.id] = time.monotonic()
            self._q.put((call.priority, next(self._seq), call))

    def cancel(self, call_id: str) -> None:
        with self._state_lock:
            self._cancelled.add(call_id)
            if call_id in self._fallback_calls and self._fallback is not None:
                try:
                    self._fallback.cancel(call_id)
                except Exception as exc:  # noqa: BLE001 - report fallback cancellation failures
                    log.warning(
                        "%s fallback cancellation failed for %s: %s",
                        self.name,
                        call_id,
                        exc,
                    )

    def _current(self, generation: int) -> bool:
        return generation == self._gen

    def _breaker_open(self) -> bool:
        return self._breaker_open_until is not None and time.monotonic() < self._breaker_open_until

    def _record_failure(self, exc: Exception) -> None:
        if not isinstance(exc, RenderTimeout):
            self.render_failures += 1
        self.last_error = str(exc)
        self._last_error_t = time.monotonic()
        self._consecutive_failures += 1
        if self._consecutive_failures >= BREAKER_FAILURES:
            self._breaker_open_until = time.monotonic() + BREAKER_OPEN_S

    def _render_primary(
        self, key: tuple[int, str], text: str, priority: int
    ) -> tuple[bytes, float]:
        lane = self._lane
        if lane is None:
            wav, _ = self._renderer.render(text, priority, background=False)
        else:
            with self._cache_lock:
                if key in self._late or len(self._late) >= MAX_STUCK_RENDERS:
                    raise RenderSkipped(f"{self.name} has stuck renders")
            future = lane.submit(lambda: self._renderer.render(text, priority, background=False))
            try:
                wav, _ = future.result(timeout=self._render_timeout_s)
            except TimeoutError:
                if future.done():
                    raise
                self.render_timeouts += 1
                with self._cache_lock:
                    self._late.add(key)
                lane.close()
                self._lane = _RenderLane()
                future.add_done_callback(
                    lambda done: self._keep_late_render(key, text, priority, done)
                )
                raise RenderTimeout(f"{self.name} render timed out") from None
        result = wav, wav_info(wav)
        self._remember(key, result, disk=True, disk_required=False)
        return result

    def _keep_late_render(
        self, key: tuple[int, str], text: str, priority: int, future: Future[tuple[bytes, float]]
    ) -> None:
        with self._cache_lock:
            self._late.discard(key)
        try:
            wav, _ = future.result()
            self._remember(key, (wav, wav_info(wav)), disk=True, disk_required=False)
        except Exception as exc:  # noqa: BLE001 - a late render is best effort
            log.warning("%s late render failed for %r: %s", self.name, text, exc)
            self._queue_background(key, text, priority)

    def _provisional_audio(
        self, provisional: Callable[[str, int], tuple[bytes, float] | None], call: Call
    ) -> tuple[bytes, float] | None:
        lane = self._provisional_lane
        if lane is None:
            return provisional(call.text, call.priority)
        job = self._provisional_job
        if job is not None and not job.done():
            return None
        job = lane.submit(lambda: provisional(call.text, call.priority))
        self._provisional_job = job
        return job.result(timeout=self._render_timeout_s)

    def _get_for_call(self, call: Call) -> tuple[bytes, float]:
        key = self._key(call.text, call.priority)
        hit = self._cached(key)
        if hit is not None:
            return hit
        try:
            if self._breaker_open():
                raise RenderSkipped(f"{self.name} circuit is open")
            result = self._render_primary(key, call.text, call.priority)
        except Exception as exc:  # noqa: BLE001 - use provisional audio, or let the call fail
            if not isinstance(exc, RenderSkipped):
                self._record_failure(exc)
            provisional = getattr(self._renderer, "provisional", None)
            audio = None
            if callable(provisional):
                try:
                    audio = self._provisional_audio(provisional, call)
                except Exception as provisional_exc:  # noqa: BLE001 - the call fails as if none existed
                    log.warning(
                        "%s provisional audio failed for %r: %s",
                        self.name,
                        call.text,
                        provisional_exc,
                    )
            if not isinstance(exc, RenderTimeout):
                self._queue_background(key, call.text, call.priority)
            if audio is None:
                raise
            log.warning(
                "%s render did not finish for %r; playing provisional audio: %s",
                self.name,
                call.text,
                exc,
            )
            self.provisional_plays += 1
            return audio[0], wav_info(audio[0])
        self._consecutive_failures = 0
        self._breaker_open_until = None
        return result

    def _queue_background(self, key: tuple[int, str], text: str, priority: int) -> None:
        with self._cache_lock:
            if (
                key not in self._pending
                and key not in self._late
                and len(self._pending) < BACKGROUND_MAX
            ):
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
                    start = time.monotonic()
                    wav, _ = self._renderer.render(text, priority, background=True)
                    elapsed = time.monotonic() - start
                    self._remember(key, (wav, wav_info(wav)), disk=True, disk_required=False)
                    log.debug(
                        "%s background render complete in %.2fs for %r",
                        self.name,
                        elapsed,
                        text,
                    )
            except Exception as exc:  # noqa: BLE001 - report background synthesis failures
                log.warning("%s background synthesis failed for %r: %s", self.name, text, exc)
            finally:
                with self._cache_lock:
                    self._pending.discard(key)

    def _failed(self, call: Call, generation: int, received: float, exc: Exception) -> None:
        with self._state_lock:
            if not self._current(generation):
                return
            if call.id in self._cancelled:
                self._cancelled.discard(call.id)
                return
            if (
                call.max_age_s is not None
                and call.handoff_age_s + (time.monotonic() - received) > call.max_age_s
            ):
                self._drop(call.id, generation)
                return
            fallback = self._fallback
            if fallback is not None:
                log.warning(
                    "%s synthesis failed; using %s as fallback: %s",
                    self.name,
                    getattr(fallback, "name", type(fallback).__name__),
                    exc,
                )
                self._fallback_calls[call.id] = generation
                try:
                    fallback.speak(call)
                except Exception as fallback_exc:  # noqa: BLE001 - report fallback playback failures
                    log.warning("%s fallback failed: %s", self.name, fallback_exc)
                    fallback_generation = self._fallback_calls.pop(call.id, None)
                    if fallback_generation is not None:
                        self._drop(call.id, fallback_generation)
                return
            log.warning("%s synthesis failed: %s", self.name, exc)
            self._drop(call.id, generation)

    def _fallback_spoken(self, call_id: str, spoken_at: float) -> None:
        with self._state_lock:
            generation = self._fallback_calls.pop(call_id, None)
            if generation is None or not self._current(generation):
                return
            if call_id in self._cancelled:
                self._cancelled.discard(call_id)
                return
            callback = self.on_spoken
            if callback is not None:
                try:
                    callback(call_id, spoken_at)
                except Exception:  # noqa: BLE001 - report callback failures and finish cleanup
                    log.exception("%s fallback spoken callback failed", self.name)

    def _fallback_dropped(self, call_id: str) -> None:
        with self._state_lock:
            generation = self._fallback_calls.pop(call_id, None)
            if generation is None or not self._current(generation):
                return
            if call_id in self._cancelled:
                self._cancelled.discard(call_id)
                return
        self._drop(call_id, generation)

    def _drop(self, call_id: str, generation: int) -> None:
        with self._state_lock:
            if not self._current(generation) or call_id in self._cancelled:
                return
            self._dropped_count += 1
            callback = self.on_dropped
            if callback is not None:
                try:
                    callback(call_id)
                except Exception:  # noqa: BLE001 - report callback failures and finish cleanup
                    log.exception("%s dropped callback failed", self.name)

    def set_volume(self, percent: int) -> None:
        self._gain = percent / 100
        setter = getattr(self._fallback, "set_volume", None)
        if callable(setter):
            setter(percent)

    def _play(self, call: Call, wav: bytes, seconds: float, generation: int) -> None:
        try:
            if self._gain != 1.0:
                with wave.open(io.BytesIO(wav), "rb") as reader:
                    params = reader.getparams()
                    frames = reader.readframes(params.nframes)
                if params.sampwidth == 2:
                    samples = np.frombuffer(frames, dtype="<i2")
                    scaled = np.clip(samples.astype(np.float64) * self._gain, -32768, 32767)
                    buf = io.BytesIO()
                    with wave.open(buf, "wb") as writer:
                        writer.setparams(params)
                        writer.writeframes(scaled.astype("<i2").tobytes())
                    wav = buf.getvalue()
        except Exception as exc:  # noqa: BLE001 - report playback failure and drop the call
            log.warning("%s playback failed: %s", self.name, exc)
            self._drop(call.id, generation)
            return
        with self._state_lock:
            if not self._current(generation) or call.id in self._cancelled:
                return
            try:
                self._player.play(wav)
            except Exception as exc:  # noqa: BLE001 - report playback failure and drop the call
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
                except Exception:  # noqa: BLE001 - report stream callback failures and continue
                    log.exception("%s audio stream failed", self.name)
        with self._state_lock:
            if (
                self._current(generation)
                and call.id not in self._cancelled
                and self.on_spoken is not None
            ):
                try:
                    self.on_spoken(call.id, time.time())
                except Exception:  # noqa: BLE001 - report spoken callback failures and continue
                    log.exception("%s spoken callback failed", self.name)

    def _worker(self) -> None:
        while True:
            _, _, call = self._q.get()
            if call is None:
                return
            try:
                self._process(call)
            except Exception:  # noqa: BLE001 - keep the worker alive for the next call
                log.exception("%s failed while handling %s", self.name, call.id)
                self._drop(call.id, self._gen)

    def _process(self, call: Call) -> None:
        with self._state_lock:
            generation = self._gen
            received = self._received.pop(call.id, time.monotonic())
            cancelled = call.id in self._cancelled
            if call.priority == 1:
                self._urgent.clear()
            if cancelled:
                self._cancelled.discard(call.id)
        if cancelled:
            return
        try:
            wav, seconds = self._get_for_call(call)
        except Exception as exc:  # noqa: BLE001 - report render failures to the call handler
            self._failed(call, generation, received, exc)
            return
        with self._state_lock:
            if not self._current(generation) or call.id in self._cancelled:
                self._cancelled.discard(call.id)
                return
            if (
                call.max_age_s is not None
                and call.handoff_age_s + (time.monotonic() - received) > call.max_age_s
            ):
                drop = True
            else:
                drop = False
        if drop:
            self._drop(call.id, generation)
            return
        with self._state_lock:
            if call.priority == 1:
                self._urgent.clear()
            self._wake.clear()
            if self._urgent.is_set():
                self._wake.set()
        self._play(call, wav, seconds, generation)
        if self._wake.wait(seconds):
            with self._state_lock:
                if self._current(generation) and self._urgent.is_set():
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
            self._wake.set()
            self._player.stop()
            if self._fallback is not None:
                from pitwall.audio.speaker import FlushableSpeaker

                if isinstance(self._fallback, FlushableSpeaker):
                    try:
                        self._fallback.flush()
                    except Exception as exc:  # noqa: BLE001 - report fallback flush failures
                        log.warning("%s fallback flush failed: %s", self.name, exc)
                else:
                    for call_id in tuple(self._fallback_calls):
                        try:
                            self._fallback.cancel(call_id)
                        except Exception as exc:  # noqa: BLE001 - report fallback cancellation failures
                            log.warning(
                                "%s fallback cancellation failed for %s: %s",
                                self.name,
                                call_id,
                                exc,
                            )
            self._fallback_calls.clear()

    def close(self) -> None:
        self._q.put((-1, next(self._seq), None))
        self._thread.join(timeout=2.0)
        self._background_queue.put(None)
        self._background_thread.join(timeout=2.0)
        if self._lane is not None:
            self._lane.close()
        if self._provisional_lane is not None:
            self._provisional_lane.close()
        self._player.stop()
        if self._fallback is not None:
            self._fallback.close()
