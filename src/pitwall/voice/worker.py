"""Run SAPI recognition on a dedicated thread."""

from __future__ import annotations

import logging
import queue
import sys
import threading
from collections.abc import Callable
from typing import Protocol

from pitwall.config.models import VoiceSettings

log = logging.getLogger(__name__)

VoiceEvent = tuple[str, str, float, float]
EventSink = Callable[[str, str, float, float], None]


class StaRecognizer(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def pump(self) -> None: ...

    def close(self) -> None: ...


class VoiceWorker:
    def __init__(
        self,
        make_recognizer: Callable[[EventSink], StaRecognizer],
        *,
        com: bool = sys.platform == "win32",
    ) -> None:
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.recognizer: StaRecognizer | None = None
        self._make_recognizer = make_recognizer
        self._com = com
        self._cmds: queue.Queue[str | None] = queue.Queue()
        self._events: queue.Queue[VoiceEvent] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="voice")
        self._thread.start()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self.ready.wait(timeout) and self.recognizer is not None

    def start(self) -> None:
        self._cmds.put("open")

    def stop(self) -> None:
        self._cmds.put("close")

    def drain(self) -> list[VoiceEvent]:
        events: list[VoiceEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def close(self) -> None:
        self._cmds.put(None)
        self._thread.join(timeout=2.0)

    def _worker(self) -> None:
        pythoncom = None
        rec: StaRecognizer | None = None
        try:
            if self._com:
                import pythoncom as com_module  # type: ignore[import-untyped]

                pythoncom = com_module
                pythoncom.CoInitialize()

            def sink(name: str, text: str, confidence: float, t: float) -> None:
                self._events.put((name, text, confidence, t))

            rec = self._make_recognizer(sink)
            self.recognizer = rec
            self.ready.set()
            listening = False
            while True:
                rec.pump()
                try:
                    cmd = self._cmds.get(timeout=0.02 if listening else 0.25)
                except queue.Empty:
                    continue
                if cmd is None:
                    return
                if cmd == "open":
                    rec.start()
                    listening = True
                elif cmd == "close":
                    rec.stop()
                    listening = False
        except BaseException as exc:
            self.error = exc
            if not self.ready.is_set():
                self.ready.set()
            log.exception("voice: worker crashed")
            print(f"voice: worker crashed: {exc!r}", file=sys.stderr)
        finally:
            if rec is not None:
                try:
                    rec.close()
                except BaseException as exc:
                    log.exception("voice: recogniser close failed")
                    print(f"voice: recogniser close failed: {exc!r}", file=sys.stderr)
            if pythoncom is not None:
                pythoncom.CoUninitialize()


def make_voice_worker(voice: VoiceSettings) -> VoiceWorker:
    from pitwall.voice.grammar import VoiceGrammar

    grammar = VoiceGrammar.from_mapping(voice.intents)

    def make_recognizer(sink: EventSink) -> StaRecognizer:
        from .sapi import SapiRecognizer

        return SapiRecognizer(
            grammar,
            sink,
            recognizer=voice.recognizer,
            device=voice.device,
            early_close_ms=voice.early_close_ms,
            close_silence_ms=voice.close_silence_ms,
        )

    return VoiceWorker(make_recognizer)
