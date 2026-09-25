"""Speech backends. `Speaker` is a CallSink for the Dispatcher; every
implementation is non-blocking and reports `on_spoken(call_id, t)`.

`SapiSpeaker` imports pywin32 lazily so this module loads on Linux.
`speech.engine`: "auto" = sapi on Windows else null; "sapi"/"null" explicit.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from pitwall.audio.dispatcher import Call
from pitwall.config.models import SpeechSettings

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


class SapiSpeaker:
    """Windows SAPI.SpVoice on one worker thread (its own COM apartment)."""

    name = "sapi"

    def __init__(self, settings: SpeechSettings) -> None:
        if sys.platform != "win32":
            raise RuntimeError("SapiSpeaker requires Windows")
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
        self._q.put(call)

    def cancel(self, call_id: str) -> None:
        self._cancelled.add(call_id)

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=2.0)

    def _worker(self) -> None:
        import winsound

        import pythoncom  # type: ignore[import-untyped]
        import win32com.client  # type: ignore[import-untyped]

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
        while True:
            call = self._q.get()
            if call is None:
                return
            if call.id in self._cancelled:
                self._cancelled.discard(call.id)
                continue
            if s.radio_click:
                winsound.Beep(1200, 40)  # type: ignore[attr-defined]
            flags = SVSF_PURGE_BEFORE_SPEAK if call.priority == 1 else 0
            voice.Speak(call.text, flags)
            if call.priority == 1:
                voice.WaitUntilDone(-1)
            if self.on_spoken is not None:
                self.on_spoken(call.id, time.time())


def make_speaker(settings: SpeechSettings) -> Speaker:
    engine = settings.engine
    if engine == "auto":
        engine = "sapi" if sys.platform == "win32" else "null"
    if engine == "sapi":
        try:
            return SapiSpeaker(settings)
        except Exception:
            return NullSpeaker()
    return NullSpeaker()
