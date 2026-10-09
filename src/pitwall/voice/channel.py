"""Engine-owned voice channel state for menu and dashboard listening."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

CLOSED = "closed"
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
    """One channel open-to-close interval."""

    opened_t: float
    closed_t: float = 0.0
    reason: str = ""  # recognised | miss | low_confidence | tap | cap | menu | session
    text: str = ""
    intent: str | None = None
    confidence: float = 0.0
    via: str = ""
    cap_s: float = 0.0
    events: dict[str, float] = field(default_factory=dict)

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
    ) -> None:
        self._rec = recognizer
        self._intent_for = intent_for
        self.max_open_s = max_open_s
        self.confidence_min = confidence_min
        self.state = CLOSED
        self.current: ChannelRecord | None = None
        self.on_close: Callable[[ChannelRecord], None] | None = None

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    def open_for(self, t: float) -> float:
        return 0.0 if self.current is None else t - self.current.opened_t

    def open(
        self,
        t: float,
        *,
        via: str = "key",
        max_open_s: float | None = None,
    ) -> bool:
        if self.is_open:
            return False
        cap_s = max_open_s or self.max_open_s
        self.state = OPEN
        self.current = ChannelRecord(opened_t=t, via=via, cap_s=cap_s)
        self._rec.start()
        return True

    def close(self, t: float, reason: str) -> None:
        rec = self.current
        if rec is None:
            return
        self._rec.stop()
        self.state = CLOSED
        self.current = None
        rec.closed_t = t
        rec.reason = reason
        if self.on_close is not None:
            self.on_close(rec)

    def key_tap(self, t: float, *, max_open_s: float | None = None) -> None:
        if self.is_open:
            self.close(t, "tap")
        else:
            self.open(t, via="key", max_open_s=max_open_s)

    def recognised(self, text: str, confidence: float, t: float) -> None:
        self._result("recognised", Heard(text, confidence, t))

    def false_recognition(self, text: str, confidence: float, t: float) -> None:
        self._result("miss", Heard(text, confidence, t))

    def mark(self, name: str, t: float) -> None:
        if self.current is not None and name not in self.current.events:
            self.current.events[name] = t - self.current.opened_t

    def tick(self, t: float) -> None:
        if self.is_open and self.current is not None and self.open_for(t) >= self.current.cap_s:
            self.close(t, "cap")

    def _result(self, reason: str, heard: Heard) -> None:
        rec = self.current
        if rec is None:
            return
        rec.text = heard.text
        rec.confidence = heard.confidence
        if reason == "recognised":
            rec.intent = self._intent_for(heard.text)
            if rec.intent is None:
                reason = "miss"
            elif heard.confidence < self.confidence_min:
                reason = "low_confidence"
        self.close(heard.t, reason)
