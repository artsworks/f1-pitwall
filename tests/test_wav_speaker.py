from __future__ import annotations

import io
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from pitwall.audio.dispatcher import Call
from pitwall.audio.wav_speaker import (
    InvalidWav,
    WavSpeaker,
    pcm_to_wav,
    wav_info,
)
from tests.audio_contract import (
    FakePlayer,
    FakeRenderer,
    assert_pitwall_wav,
    check_speaker_contract,
    make_call,
)


def _wait_for(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met")


def test_wav_speaker_contract() -> None:
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player)
    try:
        check_speaker_contract(speaker, player)
    finally:
        speaker.close()


def test_p1_preempts_and_jumps_queue() -> None:
    renderer = FakeRenderer(duration_s=1.0)
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("long", text="long"))
        _wait_for(lambda: len(player.played) == 1)
        speaker.speak(make_call("p3", priority=3, text="p3"))
        speaker.speak(make_call("urgent", priority=1, text="urgent"))
        _wait_for(lambda: "urgent" in spoken)
        assert player.stops >= 1
        assert any(text == "urgent" for text, _, _ in renderer.calls)
    finally:
        speaker.close()


def test_flush_wakes_worker_for_post_seek_call() -> None:
    renderer = FakeRenderer(duration_s=10.0)
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    spoken: list[str] = []
    old_spoken = threading.Event()
    new_spoken = threading.Event()

    def on_spoken(call_id: str, _time: float) -> None:
        spoken.append(call_id)
        if call_id == "old":
            old_spoken.set()
        if call_id == "after-seek":
            new_spoken.set()

    speaker.on_spoken = on_spoken
    try:
        speaker.speak(make_call("old", text="old"))
        assert old_spoken.wait(1.0)
        speaker.flush()
        speaker.speak(make_call("after-seek", text="after-seek"))
        assert new_spoken.wait(0.5)
        assert spoken[-1] == "after-seek"
    finally:
        speaker.close()


def test_callback_order_and_audio_errors_are_isolated() -> None:
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player)
    events: list[tuple[str, str]] = []

    def on_audio(call_id: str, _priority: int, _wav: bytes) -> None:
        events.append(("audio", call_id))
        if call_id == "first":
            raise RuntimeError("stream unavailable")

    speaker.on_audio = on_audio
    speaker.on_spoken = lambda call_id, _time: events.append(("spoken", call_id))
    try:
        speaker.speak(make_call("first"))
        speaker.speak(make_call("second"))
        _wait_for(lambda: ("spoken", "second") in events)
        assert events == [
            ("audio", "first"),
            ("spoken", "first"),
            ("audio", "second"),
            ("spoken", "second"),
        ]
        assert len(player.played) == 2
    finally:
        speaker.close()


def test_cancel_before_and_during_playback() -> None:
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player)
    spoken: list[str] = []
    audio: list[str] = []
    play_cancelled = threading.Event()
    barrier_spoken = threading.Event()
    speaker.on_audio = lambda call_id, _priority, _wav: audio.append(call_id)

    def on_spoken(call_id: str, _time: float) -> None:
        spoken.append(call_id)
        if call_id == "barrier":
            barrier_spoken.set()

    speaker.on_spoken = on_spoken

    original_play = player.play

    def cancel_during_play(wav: bytes) -> None:
        original_play(wav)
        speaker.cancel("cancel-during-play")
        play_cancelled.set()

    player.play = cancel_during_play
    try:
        speaker.cancel("cancelled-before-play")
        speaker.speak(make_call("cancelled-before-play"))
        speaker.speak(make_call("cancel-during-play"))
        assert play_cancelled.wait(1.0)
        speaker.speak(make_call("barrier"))
        assert barrier_spoken.wait(1.0)
        assert spoken == ["barrier"]
        assert audio == ["barrier"]
        assert player.played
    finally:
        speaker.close()


def test_age_expiration_and_unbounded_age() -> None:
    renderer = FakeRenderer(delay_s=0.08)
    speaker = WavSpeaker(renderer, FakePlayer())
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("expired", max_age_s=0.01))
        _wait_for(lambda: dropped == ["expired"])
        speaker.speak(make_call("unbounded", max_age_s=None, handoff_age_s=9.9))
        _wait_for(lambda: spoken == ["unbounded"])
        assert dropped == ["expired"]
    finally:
        speaker.close()


def test_cache_warm_is_priority_aware_and_survives_restart(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, cache_dir=tmp_path)
    try:
        assert speaker.warm([("same", 1), ("same", 2), ("same", 1)]) == 2
        assert len(renderer.calls) == 2
        assert all(background for _, _, background in renderer.calls)
    finally:
        speaker.close()

    restarted_renderer = FakeRenderer()
    restarted = WavSpeaker(restarted_renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        first = restarted.render("same", 1)
        second = restarted.render("same", 2)
        assert first == second
        assert restarted_renderer.calls == []
    finally:
        restarted.close()
    assert len(list(tmp_path.glob("*.wav"))) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_non_priority_renderer_uses_one_cache_slot(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    renderer.per_priority = False
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        assert speaker.warm([("same", 1), ("same", 2)]) == 1
        assert len(renderer.calls) == 1
    finally:
        speaker.close()


def test_live_cache_write_failure_does_not_block_audio(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.wav_speaker as wav_speaker_module

    def fail_write(_path: Path, _wav: bytes) -> None:
        raise PermissionError("cache is read-only")

    monkeypatch.setattr(wav_speaker_module, "_write_cache", fail_write)
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player, cache_dir=tmp_path)
    audio: list[str] = []
    spoken: list[str] = []
    speaker.on_audio = lambda call_id, _priority, _wav: audio.append(call_id)
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("live", text="live"))
        _wait_for(lambda: spoken == ["live"])
        assert audio == ["live"]
        assert player.played
        with pytest.raises(PermissionError, match="cache is read-only"):
            speaker.warm([("warm", 2)])
    finally:
        speaker.close()


def test_background_cache_write_failure_is_best_effort(monkeypatch, tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.wav_speaker as wav_speaker_module

    class ProvisionalRenderer(FakeRenderer):
        def __init__(self) -> None:
            super().__init__()
            self.provisional_wav, self.provisional_seconds = super().render("provisional", 2)

        def provisional(self, _text: str, _priority: int) -> tuple[bytes, float]:
            return self.provisional_wav, self.provisional_seconds

    def fail_write(_path: Path, _wav: bytes) -> None:
        raise PermissionError("cache is read-only")

    monkeypatch.setattr(wav_speaker_module, "_write_cache", fail_write)
    renderer = ProvisionalRenderer()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, cache_dir=tmp_path)
    audio: list[str] = []
    spoken: list[str] = []
    speaker.on_audio = lambda call_id, _priority, _wav: audio.append(call_id)
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("provisional", text="live"))
        _wait_for(lambda: spoken == ["provisional"] and not speaker._pending)
        assert audio == ["provisional"]
        assert player.played
        assert "phrase cache write failed" in caplog.text
        assert "background synthesis failed" not in caplog.text
    finally:
        speaker.close()


def test_corrupt_disk_cache_is_regenerated_and_warm_write_is_atomic(
    tmp_path: Path,
) -> None:
    renderer = FakeRenderer()
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        assert speaker.warm([("line", 2)]) == 1
        path = speaker._phrase_path((2, "line"))
        assert path is not None
        path.write_bytes(b"broken cache")
    finally:
        speaker.close()

    repaired_renderer = FakeRenderer()
    repaired = WavSpeaker(repaired_renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        wav, seconds = repaired.render("line", 2)
        assert assert_pitwall_wav(wav) == seconds
        assert repaired_renderer.calls == [("line", 2, False)]
        assert path.read_bytes() == wav
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        repaired.close()


def test_provisional_audio_fills_cache_in_background_and_reuses_disk(
    tmp_path: Path,
) -> None:
    class ProvisionalRenderer(FakeRenderer):
        per_priority = False

        def __init__(self) -> None:
            super().__init__()
            self.background_entered = threading.Event()
            self.background_release = threading.Event()
            self.provisional_wav, _ = super().render("provisional", 2)

        def provisional(self, text: str, priority: int) -> tuple[bytes, float]:
            return self.provisional_wav, wav_info(self.provisional_wav)

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if background:
                self.background_entered.set()
                self.background_release.wait(2.0)
            return super().render(text, priority, background=background)

    renderer = ProvisionalRenderer()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, cache_dir=tmp_path)
    try:
        speaker.speak(make_call("provisional", text="line"))
        _wait_for(lambda: bool(player.played))
        assert player.played == [renderer.provisional_wav]
        assert renderer.background_entered.wait(1.0)
        renderer.background_release.set()
        path = speaker._phrase_path((0, "line"))
        assert path is not None
        _wait_for(path.is_file)
    finally:
        renderer.background_release.set()
        speaker.close()

    restarted_renderer = ProvisionalRenderer()
    restarted = WavSpeaker(restarted_renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        assert restarted.render("line", 1)[0] == renderer.provisional_wav
        assert restarted_renderer.calls == [("provisional", 2, False)]
    finally:
        restarted_renderer.background_release.set()
        restarted.close()


def test_render_failure_falls_back_with_same_call_and_callbacks() -> None:
    renderer = FakeRenderer(fail_text="bad")
    player = FakePlayer()
    fallback = _Fallback()
    speaker = WavSpeaker(renderer, player, fallback=fallback)
    spoken: list[str] = []
    dropped: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    speaker.on_dropped = dropped.append
    call = make_call("same-call", text="bad")
    try:
        speaker.speak(call)
        _wait_for(lambda: bool(fallback.calls))
        assert fallback.calls == [call]
        assert fallback.on_spoken is not None
        assert fallback.on_dropped is not None
        assert spoken == ["same-call"]
        assert dropped == []
    finally:
        speaker.close()
    assert fallback.closed


def test_fallback_callbacks_are_correlated_once_and_cleared_by_flush() -> None:
    renderer = FakeRenderer(fail_text="bad")
    fallback = _DeferredFallback()
    speaker = WavSpeaker(renderer, FakePlayer(), fallback=fallback)
    spoken: list[str] = []
    dropped: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    speaker.on_dropped = dropped.append
    try:
        speaker.speak(make_call("first", text="bad"))
        speaker.speak(make_call("second", text="bad"))
        _wait_for(lambda: len(fallback.calls) == 2)

        fallback.fire_spoken("first")
        fallback.fire_spoken("first")
        fallback.fire_spoken("second")
        assert spoken == ["first", "second"]
        assert dropped == []

        speaker.speak(make_call("dropped", text="bad"))
        _wait_for(lambda: len(fallback.calls) == 3)
        fallback.fire_dropped("dropped")
        fallback.fire_dropped("dropped")
        fallback.fire_spoken("dropped")
        assert dropped == ["dropped"]
        assert spoken == ["first", "second"]

        speaker.speak(make_call("before-flush", text="bad"))
        _wait_for(lambda: len(fallback.calls) == 4)
        speaker.flush()
        fallback.fire_spoken("before-flush")
        assert spoken == ["first", "second"]
    finally:
        speaker.close()


def test_fallback_speak_error_drops_call_once() -> None:
    class RaisingFallback(_Fallback):
        def speak(self, call: Call) -> None:
            self.calls.append(call)
            raise RuntimeError("fallback unavailable")

    fallback = RaisingFallback()
    speaker = WavSpeaker(FakeRenderer(fail_text="bad"), FakePlayer(), fallback=fallback)
    dropped: list[str] = []
    speaker.on_dropped = dropped.append
    try:
        speaker.speak(make_call("failed", text="bad"))
        _wait_for(lambda: dropped == ["failed"])
        assert speaker._fallback_calls == {}
    finally:
        speaker.close()


def test_cancel_during_failed_render_does_not_forward_to_fallback() -> None:
    renderer = FakeRenderer(fail_text="bad")
    renderer.block_next()
    fallback = _DeferredFallback()
    speaker = WavSpeaker(renderer, FakePlayer(), fallback=fallback)
    dropped: list[str] = []
    barrier_spoken = threading.Event()
    speaker.on_dropped = dropped.append

    def on_spoken(call_id: str, _time: float) -> None:
        if call_id == "barrier":
            barrier_spoken.set()

    speaker.on_spoken = on_spoken
    try:
        speaker.speak(make_call("cancelled", text="bad"))
        assert renderer.entered.wait(1.0)
        speaker.cancel("cancelled")
        renderer.release.set()
        speaker.speak(make_call("barrier", text="barrier"))

        assert barrier_spoken.wait(1.0)
        assert fallback.calls == []
        assert dropped == []
    finally:
        renderer.release.set()
        speaker.close()


def test_failed_render_expiring_during_synthesis_is_dropped() -> None:
    renderer = FakeRenderer(fail_text="bad")
    renderer.block_next()
    fallback = _DeferredFallback()
    speaker = WavSpeaker(renderer, FakePlayer(), fallback=fallback)
    dropped: list[str] = []
    speaker.on_dropped = dropped.append
    try:
        speaker.speak(make_call("expired", text="bad", max_age_s=0.01))
        assert renderer.entered.wait(1.0)
        time.sleep(0.03)
        renderer.release.set()

        _wait_for(lambda: dropped == ["expired"])
        assert fallback.calls == []
    finally:
        renderer.release.set()
        speaker.close()


@pytest.mark.parametrize("flushable", [False, True])
def test_cancel_and_flush_reach_forwarded_fallback(flushable: bool) -> None:
    class FlushableFallback(_DeferredFallback):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1

    fallback = FlushableFallback() if flushable else _DeferredFallback()
    speaker = WavSpeaker(FakeRenderer(fail_text="bad"), FakePlayer(), fallback=fallback)
    try:
        speaker.speak(make_call("cancel-forwarded", text="bad"))
        _wait_for(lambda: len(fallback.calls) == 1)
        speaker.cancel("cancel-forwarded")
        assert fallback.cancelled == ["cancel-forwarded"]
        fallback.fire_dropped("cancel-forwarded")

        speaker.speak(make_call("flush-forwarded", text="bad"))
        _wait_for(lambda: len(fallback.calls) == 2)
        speaker.flush()
        if flushable:
            assert fallback.flushes == 1
            assert fallback.cancelled == ["cancel-forwarded"]
        else:
            assert fallback.cancelled == ["cancel-forwarded", "flush-forwarded"]
        assert speaker._fallback_calls == {}
    finally:
        speaker.close()


class _Fallback:
    name = "fallback"

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.on_spoken = None
        self.on_dropped = None
        self.closed = False
        self.cancelled: list[str] = []

    def speak(self, call: Call) -> None:
        self.calls.append(call)
        if self.on_spoken is not None:
            self.on_spoken(call.id, time.time())

    def cancel(self, call_id: str) -> None:
        self.cancelled.append(call_id)

    def close(self) -> None:
        self.closed = True


class _DeferredFallback(_Fallback):
    def speak(self, call: Call) -> None:
        self.calls.append(call)

    def fire_spoken(self, call_id: str) -> None:
        if self.on_spoken is not None:
            self.on_spoken(call_id, time.time())

    def fire_dropped(self, call_id: str) -> None:
        if self.on_dropped is not None:
            self.on_dropped(call_id)


@pytest.mark.parametrize("text", ["bad-render", "bad-wav"])
def test_render_failure_without_fallback_drops_once(text: str) -> None:
    renderer = FakeRenderer(
        fail_text="bad-render",
        invalid_text="bad-wav",
    )
    speaker = WavSpeaker(renderer, FakePlayer())
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call(text, text=text))
        _wait_for(lambda: dropped == [text])
        assert spoken == []
    finally:
        speaker.close()


def test_queue_status_reports_queued_calls_and_oldest_age() -> None:
    renderer = FakeRenderer()
    renderer.block_next()
    speaker = WavSpeaker(renderer, FakePlayer())
    try:
        speaker.speak(make_call("rendering", text="rendering"))
        assert renderer.entered.wait(1.0)
        speaker.speak(make_call("queued", text="queued"))
        count, oldest = speaker.queue_status()
        assert count == 1
        assert 0 <= oldest < 2
        renderer.release.set()
        _wait_for(lambda: speaker.queue_status()[0] == 0)
    finally:
        renderer.release.set()
        speaker.close()


def test_pcm_translation_downmixes_interleaved_float_chunks() -> None:
    chunks = [
        np.array([[-1.0, 1.0], [0.5, 0.5]], dtype=np.float32),
        np.array([[0.0, 0.0], [-0.5, -0.5]], dtype=np.float32),
    ]
    wav, seconds = pcm_to_wav(chunks, 16_000, channels=2, dtype="float32")
    assert seconds == pytest.approx(4 / 16_000)
    assert assert_pitwall_wav(wav) == seconds
    with wave.open(io.BytesIO(wav), "rb") as reader:
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    assert samples.tolist() == [0, 16383, 0, -16383]


def test_pcm_translation_adapter_preserves_chunk_order() -> None:
    class CallbackRenderer:
        label = "callback"
        cache_identity = "callback-v1"
        per_priority = False

        @staticmethod
        def synthesize(text: str, on_chunk) -> None:  # type: ignore[no-untyped-def]
            on_chunk(np.array([[0.0, 0.0], [0.5, 0.5]], dtype=np.float32))
            on_chunk(np.array([[0.0, 0.0], [-0.5, -0.5]], dtype=np.float32))

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            chunks: list[np.ndarray] = []
            self.synthesize(text, chunks.append)
            return pcm_to_wav(chunks, 16_000, channels=2, dtype="float32")

    player = FakePlayer()
    speaker = WavSpeaker(CallbackRenderer(), player)
    streamed: list[bytes] = []
    speaker.on_audio = lambda _call_id, _priority, wav: streamed.append(wav)
    try:
        speaker.speak(make_call("translated"))
        _wait_for(lambda: len(streamed) == 1)
        assert_pitwall_wav(streamed[0])
        with wave.open(io.BytesIO(streamed[0]), "rb") as reader:
            samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        assert samples.tolist() == [0, 16383, 0, -16383]
    finally:
        speaker.close()


def test_wav_info_rejects_invalid_and_truncated_wavs() -> None:
    assert wav_info(FakeRenderer().render("valid", 2)[0]) == pytest.approx(0.01)
    with pytest.raises(InvalidWav):
        wav_info(b"not wav")

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\0\0" * 4)
    with pytest.raises(InvalidWav, match="one channel"):
        wav_info(output.getvalue())

    valid = FakeRenderer().render("valid", 2)[0]
    with pytest.raises(InvalidWav):
        wav_info(valid[:-4])
