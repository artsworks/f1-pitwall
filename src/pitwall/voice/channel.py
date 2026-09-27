"""Talk-toggle channel driven by UDP Action 1 (docs/21).

```
closed ─ Action 1 down ─▶ provisional ─┬─ press resolves "ack" (single tap) ─▶ open
   ▲                     (recogniser    ├─ "neg" (double) / long press ─▶ closed, abort
   │                      already on)   └─ recognised before resolution: kept pending
   └── recognised / miss / second tap / cap ◀──────────────────────── open
```

The recogniser starts on the *down* edge so the first word is not lost to the
single-vs-double window; a double tap or a long press aborts the channel and
passes through to its doc 12 meaning. While open, any down edge closes it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pitwall.input.press import Press, PressDetector

CLOSED = "closed"
PROVISIONAL = "provisional"
OPEN = "open"


class Recognizer(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Heard:
    text: str
    confidence: float
    t: float


@dataclass(slots=True)
class ChannelRecord:
    """One channel open -> close, as the spike logs it."""

    opened_t: float
    closed_t: float = 0.0
    reason: str = ""  # recognised | miss | low_confidence | tap | cap | abort
    text: str = ""
    intent: str | None = None
    confidence: float = 0.0
    passthrough: str = ""  # "neg" / "silent" / "bookmark" when a gesture aborted
    events: dict[str, float] = field(default_factory=dict)  # name -> seconds after open

    @property
    def open_ms(self) -> float:
        return (self.closed_t - self.opened_t) * 1000.0


class VoiceChannel:
    def __init__(
        self,
        recognizer: Recognizer,
        intent_for: Callable[[str], str | None],
        *,
        max_open_s: float = 6.0,
        confidence_min: float = 0.7,
        detector: PressDetector | None = None,
    ) -> None:
        self._rec = recognizer
        self._intent_for = intent_for
        self.max_open_s = max_open_s
        self.confidence_min = confidence_min
        self.detector = detector or PressDetector()
        self.state = CLOSED
        self.current: ChannelRecord | None = None
        self._pending: tuple[str, Heard] | None = None  # result that beat the press window
        self._swallow_up = False
        self.on_close: Callable[[ChannelRecord], None] | None = None

    @property
    def is_open(self) -> bool:
        return self.state != CLOSED

    def open_for(self, t: float) -> float:
        return 0.0 if self.current is None else t - self.current.opened_t

    # ---------------------------------------------------------------- inputs

    def button_edge(self, t: float, down: bool) -> None:
        """UDP Action 1 edge."""
        if self.state == OPEN:
            if down:
                self._swallow_up = True
                self._close(t, "tap")
            return
        if not down and self._swallow_up:
            self._swallow_up = False
            return
        if down and self.state == CLOSED:
            self._open(t, PROVISIONAL)
        press = self.detector.edge(t, down)
        if press is not None:
            self._resolve(press)

    def key_tap(self, t: float) -> None:
        """Keyboard toggle: no gestures, a tap is a tap."""
        if self.state == CLOSED:
            self._open(t, OPEN)
        else:
            self._close(t, "tap")

    def recognised(self, text: str, confidence: float, t: float) -> None:
        self._result("recognised", Heard(text, confidence, t))

    def false_recognition(self, text: str, confidence: float, t: float) -> None:
        self._result("miss", Heard(text, confidence, t))

    def mark(self, name: str, t: float) -> None:
        """Timestamp a recogniser event (sound_start, phrase_start, ...)."""
        if self.current is not None and name not in self.current.events:
            self.current.events[name] = t - self.current.opened_t

    def tick(self, t: float) -> None:
        press = self.detector.tick(t)
        if press is not None:
            self._resolve(press)
        if self.state != CLOSED and self.open_for(t) >= self.max_open_s:
            self._close(t, "cap")

    # ---------------------------------------------------------------- internals

    def _open(self, t: float, state: str) -> None:
        self.state = state
        self.current = ChannelRecord(opened_t=t)
        self._pending = None
        self._rec.start()

    def _resolve(self, press: Press) -> None:
        if self.state != PROVISIONAL:
            return
        if press.kind == "ack":
            self.state = OPEN
            if self._pending is not None:
                reason, heard = self._pending
                self._pending = None
                self._finish(reason, heard)
            return
        assert self.current is not None
        self.current.passthrough = press.kind
        self._close(press.t, "abort")

    def _result(self, reason: str, heard: Heard) -> None:
        if self.state == CLOSED:
            return
        if self.state == PROVISIONAL:
            self._pending = (reason, heard)
            return
        self._finish(reason, heard)

    def _finish(self, reason: str, heard: Heard) -> None:
        assert self.current is not None
        rec = self.current
        rec.text = heard.text
        rec.confidence = heard.confidence
        if reason == "recognised":
            rec.intent = self._intent_for(heard.text)
            if rec.intent is None:
                reason = "miss"
            elif heard.confidence < self.confidence_min:
                reason = "low_confidence"
        self._close(heard.t, reason)

    def _close(self, t: float, reason: str) -> None:
        rec = self.current
        self._rec.stop()
        self.state = CLOSED
        self.current = None
        self._pending = None
        if rec is None:
            return
        rec.closed_t = t
        rec.reason = reason
        if reason not in ("recognised", "low_confidence"):
            rec.intent = None
        if self.on_close is not None:
            self.on_close(rec)
