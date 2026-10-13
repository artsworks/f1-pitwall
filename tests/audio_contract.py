from __future__ import annotations

import io
import threading
import time
import wave

import numpy as np

from pitwall.audio.dispatcher import Call
from pitwall.audio.wav_speaker import to_wav, wav_info


def make_call(
    cid: str,
    priority: int = 2,
    text: str = "box box",
    max_age_s: float | None = None,
    handoff_age_s: float = 0.0,
) -> Call:
    now = time.monotonic()
    return Call(
        id=cid,
        rule_id=cid,
        priority=priority,
        text=text,
        tags=[],
        deadline_ms=5000,
        lap=1,
        t=now,
        trigger_t=now,
        max_age_s=max_age_s,
        handoff_age_s=handoff_age_s,
    )


def assert_pitwall_wav(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() > 0
        assert reader.getnframes() > 0
    return wav_info(wav)


class FakeRenderer:
    label = "fake"
    cache_identity = "fake-v1"
    per_priority = True

    def __init__(
        self,
        *,
        duration_s: float = 0.01,
        delay_s: float = 0.0,
        fail_text: str | None = None,
        invalid_text: str | None = None,
    ) -> None:
        self.duration_s = duration_s
        self.delay_s = delay_s
        self.fail_text = fail_text
        self.invalid_text = invalid_text
        self.calls: list[tuple[str, int, bool]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._block_next = False

    def block_next(self) -> None:
        self.entered.clear()
        self.release.clear()
        self._block_next = True

    def render(self, text: str, priority: int, *, background: bool = False) -> tuple[bytes, float]:
        self.calls.append((text, priority, background))
        if self._block_next:
            self._block_next = False
            self.entered.set()
            self.release.wait(2.0)
        if self.delay_s:
            time.sleep(self.delay_s)
        if text == self.fail_text:
            raise RuntimeError("render failed")
        if text == self.invalid_text:
            return b"not a wav", 1.0
        count = max(1, round(16_000 * self.duration_s))
        samples = np.sin(np.arange(count) * 2 * np.pi * 440 / 16_000)
        return to_wav(samples, 16_000), count / 16_000


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.stops = 0

    def play(self, wav: bytes) -> None:
        self.played.append(wav)

    def stop(self) -> None:
        self.stops += 1


def _wait_for(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met")


def check_speaker_contract(speaker, player=None) -> None:  # type: ignore[no-untyped-def]
    events: list[tuple[str, str]] = []
    dropped: list[str] = []

    def on_audio(call_id: str, _priority: int, wav: bytes) -> None:
        assert_pitwall_wav(wav)
        events.append(("audio", call_id))

    def on_spoken(call_id: str, _timestamp: float) -> None:
        events.append(("spoken", call_id))

    speaker.on_audio = on_audio
    speaker.on_spoken = on_spoken
    if hasattr(speaker, "on_dropped"):
        speaker.on_dropped = dropped.append

    speaker.speak(make_call("contract-p2"))
    speaker.speak(make_call("contract-p1", priority=1))
    _wait_for(lambda: {"contract-p2", "contract-p1"} <= {cid for _, cid in events})
    for call_id in ("contract-p2", "contract-p1"):
        assert events.count(("audio", call_id)) == 1
        assert events.count(("spoken", call_id)) == 1
        assert events.index(("audio", call_id)) < events.index(("spoken", call_id))

    speaker.cancel("contract-cancelled")
    speaker.speak(make_call("contract-cancelled"))
    speaker.speak(make_call("contract-barrier", text="barrier"))
    _wait_for(lambda: ("spoken", "contract-barrier") in events)
    assert not any(call_id == "contract-cancelled" for _, call_id in events)

    renderer = getattr(speaker, "_renderer", None)
    if hasattr(renderer, "block_next"):
        renderer.block_next()
        speaker.speak(make_call("contract-flush-inflight", text="flush in flight"))
        assert renderer.entered.wait(1.0)
        speaker.speak(make_call("contract-flush-queued", text="flush queued"))
        speaker.flush()
        renderer.release.set()
        speaker.speak(make_call("contract-after-flush", text="after flush"))
        _wait_for(lambda: ("spoken", "contract-after-flush") in events)
        assert not any(
            call_id in ("contract-flush-inflight", "contract-flush-queued") for _, call_id in events
        )

    speaker.speak(make_call("contract-expired", max_age_s=0.0, handoff_age_s=1.0))
    _wait_for(lambda: "contract-expired" in dropped)
    assert dropped.count("contract-expired") == 1
    assert not any(call_id == "contract-expired" for _, call_id in events)
