"""The speech contract.

Engines receive the whole dispatcher Call. They must honor its id, priority,
handoff_age_s, and max_age_s. They may read deadline_ms, tags, lap, urgency,
and rank. The dispatcher enforces screen_only through speaks_audio and owns
still_true. on_spoken marks playback start.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from pitwall.audio.dispatcher import Call
from pitwall.config.models import SpeechSettings

if TYPE_CHECKING:
    from pitwall.audio.dispatcher import Dispatcher
    from pitwall.server.hub import Hub

log = logging.getLogger(__name__)

SVSF_ASYNC = 1
SVSF_PURGE_BEFORE_SPEAK = 4
STARTUP_LINES: tuple[tuple[str, str], ...] = (
    ("startup", "Pit wall online."),
    ("startup-radio-check", "Radio check, radio check."),
)
STARTUP_PRIORITY = 3
REJOIN_OPENER = "Back with you."


class Speaker(Protocol):
    name: str
    speaks_audio: bool  # audio sinks skip screen-only calls
    supports_audio_stream: bool
    on_spoken: Callable[[str, float], None] | None

    def speak(self, call: Call) -> None: ...

    def cancel(self, call_id: str) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class StreamableSpeaker(Speaker, Protocol):
    on_audio: Callable[[str, int, bytes], None] | None


@runtime_checkable
class OutcomeSpeaker(Protocol):
    on_dropped: Callable[[str], None] | None


@runtime_checkable
class WarmableSpeaker(Protocol):
    def warm(self, phrases: list[tuple[str, int]]) -> int: ...


@runtime_checkable
class FlushableSpeaker(Protocol):
    def flush(self) -> None: ...


@runtime_checkable
class QueueStatusSpeaker(Protocol):
    def queue_status(self) -> tuple[int, float]: ...


def streams_audio(speaker: Speaker) -> bool:
    if getattr(speaker, "supports_audio_stream", False) is not True:
        return False
    if not hasattr(speaker, "on_audio"):
        log.warning("%s claims audio streaming but has no on_audio callback", speaker.name)
        return False
    return True


def wire_speaker(speaker: Speaker, hub: Hub, dispatcher: Dispatcher) -> None:
    def on_spoken(call_id: str, spoken_at: float) -> None:
        hub.spoken(call_id, spoken_at)
        dispatcher.metrics.note_spoken(call_id, spoken_at)

    speaker.on_spoken = on_spoken
    if isinstance(speaker, OutcomeSpeaker):

        def on_dropped(call_id: str) -> None:
            dispatcher.note_dropped(call_id)
            hub.cancel(call_id)

        speaker.on_dropped = on_dropped
    hub.streams_audio = streams_audio(speaker)
    if hub.streams_audio:
        cast(StreamableSpeaker, speaker).on_audio = hub.audio


class NullSpeaker:
    name = "null"
    speaks_audio = True
    supports_audio_stream = False
    on_spoken: Callable[[str, float], None] | None = None

    def speak(self, call: Call) -> None:
        if self.on_spoken is not None:
            self.on_spoken(call.id, time.time())

    def cancel(self, call_id: str) -> None:
        pass

    def close(self) -> None:
        pass


def _speak_one(
    voice: Any,
    call: Call,
    urgent: threading.Event,
    on_spoken: Callable[[str, float], None] | None,
    blip: bytes | None,
) -> bool:
    """Speak one call on a SAPI voice; return True if speech finished normally
    (False if cut short by `urgent`). Async speak keeps the worker free to
    dequeue a P1 purge.

    - P1 -> PURGE|ASYNC so it cuts whatever is speaking, then ASYNC for the rest.
    - on_spoken fires right after Speak returns (speech start).
    - WaitUntilDone(50) returns True when done; urgent breaks the wait.
    - Play the blip only when nothing is already playing (previous wait finished
      normally): the P1 purge already cuts audio, and the blip would delay it.
    """
    if blip is not None:
        try:
            import winsound

            winsound.PlaySound(blip, winsound.SND_MEMORY)  # type: ignore[attr-defined]
        except Exception:
            pass
    flags = SVSF_ASYNC | (SVSF_PURGE_BEFORE_SPEAK if call.priority == 1 else 0)
    voice.Speak(call.text, flags)
    if on_spoken is not None:
        on_spoken(call.id, time.time())
    finished = True
    while voice.WaitUntilDone(50) is False:
        if urgent.is_set():
            finished = False
            break
    if call.priority == 1:
        urgent.clear()
    return finished


class SapiSpeaker:
    """Windows SAPI.SpVoice on one worker thread (its own COM apartment)."""

    name = "sapi"
    speaks_audio = True
    supports_audio_stream = False

    def __init__(self, settings: SpeechSettings) -> None:
        if sys.platform != "win32":
            raise RuntimeError("SapiSpeaker requires Windows")
        import winsound  # noqa: F401

        import pythoncom  # type: ignore[import-untyped, unused-ignore]  # noqa: F401
        import win32com.client  # type: ignore[import-untyped, unused-ignore]  # noqa: F401

        self._settings: SpeechSettings = settings
        self.on_spoken: Callable[[str, float], None] | None = None
        self._q: queue.Queue[Call | None] = queue.Queue()
        self._cancelled: set[str] = set()
        self._urgent: threading.Event = threading.Event()
        self._thread: threading.Thread = threading.Thread(
            target=self._worker, daemon=True, name="sapi"
        )
        self._thread.start()

    def speak(self, call: Call) -> None:
        if call.priority == 1:
            self._urgent.set()
        self._q.put(call)

    def cancel(self, call_id: str) -> None:
        self._cancelled.add(call_id)

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=2.0)

    def _worker(self) -> None:
        try:
            self._work()
        except Exception as exc:
            print(f"speech: SAPI worker crashed: {exc!r}", file=sys.stderr)
            raise

    def _work(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        voice: Any = win32com.client.Dispatch("SAPI.SpVoice")
        s = self._settings
        voice.Rate = max(-10, min(10, s.rate))
        voice.Volume = max(0, min(100, s.volume))
        if s.voice:
            for v in voice.GetVoices():
                if s.voice.lower() in v.GetDescription().lower():
                    voice.Voice = v
                    break
        if s.radio_click:
            from pitwall.audio.wav_speaker import radio_blip, to_wav

            blip = to_wav(radio_blip(22050), 22050)
        else:
            blip = None
        finished = True  # whether previous speech finished normally
        while True:
            call = self._q.get()
            if call is None:
                return
            if call.id in self._cancelled:
                self._cancelled.discard(call.id)
                continue
            finished = _speak_one(
                voice,
                call,
                self._urgent,
                self.on_spoken,
                blip if finished else None,
            )


@dataclass(frozen=True)
class EngineSpec:
    id: str
    factory: Callable[[SpeechSettings], Speaker]
    fallback: Callable[[], str] | str | None


ENGINES: dict[str, EngineSpec] = {}


def register_engine(spec: EngineSpec) -> None:
    ENGINES[spec.id] = spec


def _make_omnivoice(settings: SpeechSettings) -> Speaker:
    from pitwall.audio.omnivoice_tts import make_omnivoice_speaker

    return make_omnivoice_speaker(settings)


def _make_piper(settings: SpeechSettings) -> Speaker:
    from pitwall.audio.piper_tts import make_piper_speaker

    return make_piper_speaker(settings)


def _make_sapi(settings: SpeechSettings) -> Speaker:
    return SapiSpeaker(settings)


def _make_null(settings: SpeechSettings) -> Speaker:
    return NullSpeaker()


def _piper_fallback() -> str:
    return "sapi" if sys.platform == "win32" else "null"


register_engine(EngineSpec("omnivoice", _make_omnivoice, "piper"))
register_engine(EngineSpec("piper", _make_piper, _piper_fallback))
register_engine(EngineSpec("sapi", _make_sapi, "null"))
register_engine(EngineSpec("null", _make_null, None))


def auto_engine(settings: SpeechSettings) -> str:
    if sys.platform == "win32":
        from pitwall.audio.piper_tts import voice_path

        if voice_path(settings).exists():
            return "piper"
        return "sapi"
    return "null"


def _fallback_id(spec: EngineSpec) -> str | None:
    return spec.fallback() if callable(spec.fallback) else spec.fallback


def _failure(spec: EngineSpec, exc: Exception, next_id: str | None) -> None:
    if spec.id == "omnivoice":
        message = f"OmniVoice unavailable ({exc}), falling back to Piper"
        log.warning("OmniVoice unavailable (%s); falling back to Piper", exc)
    elif spec.id == "piper":
        next_name = (next_id or "null").upper()
        message = f"Piper unavailable ({exc}); falling back to {next_name}"
        log.warning("Piper unavailable (%s); falling back", exc)
    elif spec.id == "sapi":
        message = f"SAPI unavailable ({exc!r}); speech disabled"
        log.warning("SAPI unavailable (%s); speech disabled", exc)
    else:
        message = (
            f"{spec.id} unavailable ({exc}); falling back to {next_id}"
            if next_id is not None
            else f"{spec.id} unavailable ({exc}); speech disabled"
        )
        log.warning("%s unavailable (%s); falling back to %s", spec.id, exc, next_id)
    print(f"speech: {message}", file=sys.stderr)


def make_speaker(settings: SpeechSettings) -> Speaker:
    if not settings.enabled:
        return NullSpeaker()
    engine = settings.engine
    if engine == "auto":
        engine = auto_engine(settings)
    elif engine not in ENGINES:
        registered = ", ".join(sorted(ENGINES))
        log.warning("Unknown speech engine %r; registered engines: %s", engine, registered)
        engine = auto_engine(settings)
    visited: set[str] = set()
    while engine in ENGINES and engine not in visited:
        visited.add(engine)
        spec = ENGINES[engine]
        try:
            return spec.factory(settings)
        except Exception as exc:
            fallback = _fallback_id(spec)
            _failure(spec, exc, fallback)
            if fallback is None:
                break
            engine = fallback
    return NullSpeaker()
