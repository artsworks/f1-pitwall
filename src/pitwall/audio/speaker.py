"""Speech backends. `Speaker` is a CallSink for the Dispatcher; every
implementation is non-blocking and reports `on_spoken(call_id, t)` — fired at
speech start (the latency we measure).

`SapiSpeaker` imports pywin32 lazily so this module loads on Linux.
`speech.engine`: "auto" = sapi on Windows else null; "sapi"/"null" explicit.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from pitwall.audio.dispatcher import Call
from pitwall.config.models import SpeechSettings

log = logging.getLogger(__name__)

SVSF_ASYNC = 1
SVSF_PURGE_BEFORE_SPEAK = 4


class Speaker(Protocol):
    name: str
    on_spoken: Callable[[str, float], None] | None

    def speak(self, call: Call) -> None: ...

    def cancel(self, call_id: str) -> None: ...

    def close(self) -> None: ...


class NullSpeaker:
    name = "null"
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
    beep: bool,
) -> bool:
    """Speak one call on a SAPI voice; return True if speech finished normally
    (False if cut short by `urgent`). Async speak keeps the worker free to
    dequeue a P1 purge.

    - P1 -> PURGE|ASYNC so it cuts whatever is speaking, then ASYNC for the rest.
    - on_spoken fires right after Speak returns (speech start).
    - WaitUntilDone(50) returns True when done; urgent breaks the wait.
    - Beep only when nothing is already playing (previous wait finished
      normally): the P1 purge already cuts audio, and the beep would delay it.
    """
    if beep:
        try:
            import winsound

            winsound.Beep(1200, 40)  # type: ignore[attr-defined]
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

    def __init__(self, settings: SpeechSettings) -> None:
        if sys.platform != "win32":
            raise RuntimeError("SapiSpeaker requires Windows")
        import winsound  # noqa: F401

        import pythoncom  # type: ignore[import-untyped]  # noqa: F401
        import win32com.client  # type: ignore[import-untyped]  # noqa: F401

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
        beep_enabled = s.radio_click
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
                beep_enabled and finished,
            )


def make_speaker(settings: SpeechSettings) -> Speaker:
    engine = settings.engine
    if engine == "auto":
        engine = "sapi" if sys.platform == "win32" else "null"
    if engine == "sapi":
        try:
            return SapiSpeaker(settings)
        except Exception as exc:
            log.warning("SAPI unavailable (%s); speech disabled", exc)
            print(
                f"speech: SAPI unavailable ({exc!r}); speech disabled",
                file=sys.stderr,
            )
            return NullSpeaker()
    return NullSpeaker()
