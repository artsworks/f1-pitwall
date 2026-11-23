from __future__ import annotations

import io
import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from pitwall.audio.dispatcher import Call
from pitwall.audio.wav_speaker import (
    InvalidWav,
    WarmProcess,
    WavSpeaker,
    pcm_to_wav,
    to_wav,
    wav_info,
)
from tests.audio_contract import (
    FakePlayer,
    FakeRenderer,
    assert_pitwall_wav,
    check_speaker_contract,
    make_call,
    warm_test_init,
    warm_test_render,
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


@pytest.mark.parametrize("percent", [0, 40, 70, 100, 150])
def test_live_volume_scales_cached_audio_and_stream(percent: int) -> None:
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player)
    original = np.array([-30000, -1000, 1000, 30000], dtype="<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(original.tobytes())
    streamed: list[bytes] = []
    speaker.on_audio = lambda _id, _priority, wav: streamed.append(wav)
    try:
        speaker.set_volume(percent)
        call = make_call("gain")
        speaker._cache[speaker._key(call.text, call.priority)] = (buf.getvalue(), 0.01)
        speaker.speak(call)
        _wait_for(lambda: bool(streamed))
        assert streamed == player.played
        with wave.open(io.BytesIO(player.played[0]), "rb") as reader:
            actual = np.frombuffer(reader.readframes(4), dtype="<i2")
        expected = np.clip(original.astype(float) * percent / 100, -32768, 32767).astype("<i2")
        np.testing.assert_array_equal(actual, expected)
        assert_pitwall_wav(streamed[0])
    finally:
        speaker.close()


@pytest.mark.parametrize("sample_width", [1, 2, 3, 4])
def test_volume_preserves_wav_params(sample_width: int) -> None:
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player)
    frames = bytes([10, 20]) * (sample_width * 4)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(sample_width)
        writer.setframerate(22050)
        writer.writeframes(frames)
    original = buf.getvalue()
    try:
        speaker.set_volume(40)
        speaker._play(make_call("params"), original, 0.01, speaker._gen)
        with (
            wave.open(io.BytesIO(original), "rb") as before,
            wave.open(io.BytesIO(player.played[0]), "rb") as after,
        ):
            assert after.getparams() == before.getparams()
            if sample_width == 2:
                expected = (np.frombuffer(frames, dtype="<i2").astype(float) * 0.4).astype("<i2")
                assert after.readframes(after.getnframes()) == expected.tobytes()
            else:
                assert player.played[0] == original
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


def test_dropped_p1_does_not_stop_later_calls() -> None:
    renderer = FakeRenderer(fail_text="urgent")
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("urgent", priority=1, text="urgent"))
        _wait_for(lambda: dropped == ["urgent"])
        speaker.speak(make_call("routine", priority=2, text="routine"))
        _wait_for(lambda: spoken == ["routine"])
        time.sleep(0.05)
        assert player.stops == 0
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


def test_warm_thread_parallelism_renders_uncached_phrases_concurrently(
    tmp_path: Path,
) -> None:
    class ThreadRenderer(FakeRenderer):
        warm_parallelism = "thread"
        warm_workers = 4

        def __init__(self) -> None:
            super().__init__(delay_s=0.05)
            self._lock = threading.Lock()
            self.in_flight = 0
            self.max_in_flight = 0

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            with self._lock:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                return super().render(text, priority, background=background)
            finally:
                with self._lock:
                    self.in_flight -= 1

    renderer = ThreadRenderer()
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        phrases = [(f"line {i}", 2) for i in range(6)]
        assert speaker.warm(phrases) == 6
        assert renderer.max_in_flight >= 2
        assert len(renderer.calls) == 6
        assert all(background for _, _, background in renderer.calls)
        assert len(list(tmp_path.glob("*.wav"))) == 6

        renderer.calls.clear()
        assert speaker.warm(phrases) == 6
        assert renderer.calls == []
    finally:
        speaker.close()


def test_warm_process_parallelism_uses_worker_processes(tmp_path: Path) -> None:
    class ProcessRenderer(FakeRenderer):
        warm_parallelism = "process"
        warm_workers = 2

        def __init__(self, spec: WarmProcess) -> None:
            super().__init__()
            self.warm_process = spec

    log_path = tmp_path / "workers.log"
    renderer = ProcessRenderer(
        WarmProcess(warm_test_init, (str(log_path),), warm_test_render, min_batch=1)
    )
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path / "cache")
    try:
        phrases = [(f"line {i}", priority) for i, priority in enumerate((1, 2, 3, 1))]
        assert speaker.warm(phrases) == 4
        pids = {line.strip() for line in log_path.read_text().splitlines()}
        assert len(pids) == 2
        assert len(list((tmp_path / "cache").glob("*.wav"))) == 4
        for text, priority in phrases:
            assert speaker._cached((priority, text)) is not None
    finally:
        speaker.close()


def test_warm_process_mode_without_spec_renders_sequentially(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    renderer.warm_parallelism = "process"
    renderer.warm_workers = 4
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        assert speaker.warm([("a", 2), ("b", 2), ("c", 2)]) == 3
        assert len(renderer.calls) == 3
        assert all(background for _, _, background in renderer.calls)
    finally:
        speaker.close()


def test_warm_process_small_batch_renders_in_process(tmp_path: Path) -> None:
    class ProcessRenderer(FakeRenderer):
        warm_parallelism = "process"
        warm_workers = 2

        def __init__(self, spec: WarmProcess) -> None:
            super().__init__()
            self.warm_process = spec

    log_path = tmp_path / "workers.log"
    renderer = ProcessRenderer(WarmProcess(warm_test_init, (str(log_path),), warm_test_render))
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path / "cache")
    try:
        phrases = [(f"line {i}", 2) for i in range(4)]
        assert speaker.warm(phrases) == 4
        assert len(renderer.calls) == 4
        assert all(background for _, _, background in renderer.calls)
        assert not log_path.exists()
    finally:
        speaker.close()


def test_warm_reports_progress_for_sequential_render(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        assert speaker.warm([("cached", 2)]) == 1
        calls: list[tuple[int, int, int]] = []
        count = speaker.warm(
            [("cached", 2), ("new a", 2), ("new b", 2)],
            progress=lambda done, total, cached: calls.append((done, total, cached)),
        )
        assert count == 3
        assert calls == [(0, 2, 1), (1, 2, 1), (2, 2, 1)]
    finally:
        speaker.close()


def test_warm_reports_progress_for_thread_render(tmp_path: Path) -> None:
    class ThreadRenderer(FakeRenderer):
        warm_parallelism = "thread"
        warm_workers = 4

    renderer = ThreadRenderer(delay_s=0.02)
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        calls: list[tuple[int, int, int]] = []
        phrases = [(f"line {i}", 2) for i in range(6)]
        count = speaker.warm(
            phrases,
            progress=lambda done, total, cached: calls.append((done, total, cached)),
        )
        assert count == 6
        assert calls[0] == (0, 6, 0)
        assert calls[-1] == (6, 6, 0)
        dones = [done for done, _, _ in calls]
        assert dones == sorted(set(dones))
    finally:
        speaker.close()


def test_warm_failure_cancels_pending_and_caches_successes(tmp_path: Path) -> None:
    class FailingRenderer(FakeRenderer):
        warm_parallelism = "thread"
        warm_workers = 2

        def __init__(self) -> None:
            super().__init__(delay_s=0.05)

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if text == "line 1":
                self.calls.append((text, priority, background))
                raise RuntimeError("boom")
            return super().render(text, priority, background=background)

    renderer = FailingRenderer()
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path)
    try:
        phrases = [(f"line {i}", 2) for i in range(12)]
        with pytest.raises(RuntimeError, match="boom"):
            speaker.warm(phrases)
        calls = [text for text, _, _ in renderer.calls]
        assert len(calls) < 12
        successes = len([text for text in calls if text != "line 1"])
        assert len(list(tmp_path.glob("*.wav"))) == successes
        if "line 0" in calls:
            assert speaker._cached(speaker._key("line 0", 2)) is not None
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

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if not background:
                raise TimeoutError("foreground wait expired")
            return super().render(text, priority, background=background)

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
            if not background:
                raise TimeoutError("foreground wait expired")
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


def test_provisional_logs_background_render(caplog) -> None:  # type: ignore[no-untyped-def]
    class ProvisionalRenderer(FakeRenderer):
        def provisional(self, text: str, priority: int) -> tuple[bytes, float]:
            return super().render(text, priority)

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if not background:
                raise TimeoutError("foreground wait expired")
            return super().render(text, priority, background=background)

    caplog.set_level(logging.DEBUG, logger="pitwall.audio.wav_speaker")
    speaker = WavSpeaker(ProvisionalRenderer(), FakePlayer())
    try:
        speaker.speak(make_call("provisional-log", text="line"))
        _wait_for(
            lambda: any(
                "background render complete in" in record.getMessage() for record in caplog.records
            )
        )
        assert any(
            "background render complete in" in record.getMessage() for record in caplog.records
        )
    finally:
        speaker.close()


class WaitingRenderer(FakeRenderer):
    per_priority = False

    def __init__(self, *, delay_s: float = 0.0) -> None:
        super().__init__(delay_s=delay_s)
        self.provisional_calls = 0

    def provisional(self, _text: str, _priority: int) -> tuple[bytes, float]:
        self.provisional_calls += 1
        wav = pcm_to_wav([np.zeros(160, dtype=np.int16)], 16_000)
        return wav, wav_info(wav)


def test_provisional_renderer_plays_foreground_render_and_caches_it(tmp_path: Path) -> None:
    renderer = WaitingRenderer()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, cache_dir=tmp_path)
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("cold", text="line"))
        _wait_for(lambda: spoken == ["cold"])
        path = speaker._phrase_path((0, "line"))
        assert path is not None
        assert player.played == [path.read_bytes()]
        assert renderer.calls == [("line", 2, False)]
        assert renderer.provisional_calls == 0
        assert not speaker._pending
    finally:
        speaker.close()


def test_provisional_renderer_drops_call_that_expires_while_waiting() -> None:
    renderer = WaitingRenderer(delay_s=0.08)
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    dropped: list[str] = []
    speaker.on_dropped = dropped.append
    try:
        speaker.speak(make_call("expired", text="line", max_age_s=0.01))
        _wait_for(lambda: dropped == ["expired"])
        assert player.played == []
    finally:
        speaker.close()


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


class ProvisionalFake(FakeRenderer):
    def __init__(
        self, *, fail_text: str | None = None, provisional_error: str | None = None
    ) -> None:
        super().__init__(fail_text=fail_text)
        self.provisional_error = provisional_error
        self.provisional_wav = to_wav(np.zeros(160, dtype=np.float32), 16_000)

    def provisional(self, _text: str, _priority: int) -> tuple[bytes, float]:
        if self.provisional_error is not None:
            raise RuntimeError(self.provisional_error)
        return self.provisional_wav, wav_info(self.provisional_wav)


def test_render_timeout_plays_provisional_then_caches_late_render(tmp_path: Path) -> None:
    renderer = ProvisionalFake()
    renderer.block_next()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, cache_dir=tmp_path, render_timeout_s=0.05)
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("slow", text="line"))
        _wait_for(lambda: spoken == ["slow"])
        assert player.played == [renderer.provisional_wav]
        assert speaker.health()["render_timeouts"] == 1
        renderer.release.set()
        path = speaker._phrase_path(speaker._key("line", 2))
        assert path is not None
        _wait_for(path.is_file)
        speaker.speak(make_call("again", text="line"))
        _wait_for(lambda: spoken == ["slow", "again"])
        assert player.played[1] == path.read_bytes()
        assert renderer.calls == [("line", 2, False)]
    finally:
        renderer.release.set()
        speaker.close()


def test_look_ahead_renders_next_call_while_previous_plays() -> None:
    renderer = FakeRenderer(duration_s=1.0)
    speaker = WavSpeaker(renderer, FakePlayer())
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("a", text="a"))
        speaker.speak(make_call("b", text="b"))
        _wait_for(lambda: spoken == ["a"])
        _wait_for(lambda: any(text == "b" for text, _, _ in renderer.calls), timeout=0.5)
        assert spoken == ["a"]
        _wait_for(lambda: spoken == ["a", "b"], timeout=3.0)
    finally:
        speaker.close()


def test_uncached_call_renders_once_when_look_ahead_is_claimed() -> None:
    renderer = FakeRenderer(delay_s=0.1)
    speaker = WavSpeaker(renderer, FakePlayer())
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("once", text="once"))
        _wait_for(lambda: spoken == ["once"])
        assert [text for text, _, _ in renderer.calls] == ["once"]
    finally:
        speaker.close()


def test_p1_does_not_wait_for_unstarted_look_ahead_renders() -> None:
    renderer = FakeRenderer(duration_s=1.0)
    speaker = WavSpeaker(renderer, FakePlayer())
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("long", text="long"))
        _wait_for(lambda: spoken == ["long"])
        renderer.block_next()
        speaker.speak(make_call("x", priority=3, text="x"))
        assert renderer.entered.wait(1.0)
        speaker.speak(make_call("y", priority=3, text="y"))
        speaker.speak(make_call("z", priority=1, text="z"))
        _wait_for(lambda: "z" in spoken, timeout=0.5)
        assert "x" not in spoken
        renderer.release.set()
        _wait_for(lambda: spoken == ["long", "z", "x", "y"], timeout=4.0)
    finally:
        renderer.release.set()
        speaker.close()


def test_flush_cancels_unstarted_look_ahead_renders() -> None:
    renderer = FakeRenderer()
    renderer.block_next()
    speaker = WavSpeaker(renderer, FakePlayer())
    events: list[str] = []
    speaker.on_audio = lambda call_id, _priority, _wav: events.append(call_id)
    speaker.on_spoken = lambda call_id, _time: events.append(call_id)
    speaker.on_dropped = events.append
    try:
        speaker.speak(make_call("x", text="x"))
        assert renderer.entered.wait(1.0)
        speaker.speak(make_call("y", text="y"))
        speaker.flush()
        renderer.release.set()
        _wait_for(lambda: speaker._cached(speaker._key("x", 2)) is not None)
        time.sleep(0.05)
        assert "y" not in [text for text, _, _ in renderer.calls]
        assert events == []
    finally:
        renderer.release.set()
        speaker.close()


def test_failed_look_ahead_render_is_not_retried_in_foreground() -> None:
    renderer = FakeRenderer(fail_text="bad")
    speaker = WavSpeaker(renderer, FakePlayer())
    dropped: list[str] = []
    speaker.on_dropped = dropped.append

    def foreground() -> list[tuple[str, int, bool]]:
        return [call for call in renderer.calls if not call[2]]

    try:
        speaker.speak(make_call("bad-0", text="bad"))
        _wait_for(lambda: dropped == ["bad-0"])
        assert foreground() == [("bad", 2, False)]
        speaker.speak(make_call("bad-1", text="bad"))
        _wait_for(lambda: dropped == ["bad-0", "bad-1"])
        assert foreground() == [("bad", 2, False)] * 2
    finally:
        speaker.close()


def test_claimed_look_ahead_timeout_replaces_lane(tmp_path: Path) -> None:
    renderer = ProvisionalFake()
    renderer.block_next()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, cache_dir=tmp_path, render_timeout_s=0.05)
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("slow", text="line"))
        _wait_for(lambda: spoken == ["slow"])
        assert player.played == [renderer.provisional_wav]
        assert speaker.health()["render_timeouts"] == 1
        speaker.speak(make_call("other", text="other"))
        _wait_for(lambda: spoken == ["slow", "other"])
        renderer.release.set()
        path = speaker._phrase_path(speaker._key("line", 2))
        assert path is not None
        _wait_for(path.is_file)
    finally:
        renderer.release.set()
        speaker.close()


def _messages(caplog) -> list[str]:  # type: ignore[no-untyped-def]
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


def _diagnostic(caplog, needle: str) -> str:  # type: ignore[no-untyped-def]
    _wait_for(lambda: any(needle in message for message in _messages(caplog)))
    return next(message for message in _messages(caplog) if needle in message)


def test_slow_render_reports_duration_and_gpu(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.wav_speaker as wav_speaker_module

    monkeypatch.setattr(wav_speaker_module, "SLOW_RENDER_S", 0.0)
    monkeypatch.setattr(wav_speaker_module, "gpu_status", lambda: "gpu 50 %, 1000/16000 MB")
    speaker = WavSpeaker(FakeRenderer(), FakePlayer())
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("slow", text="line"))
        _wait_for(lambda: spoken == ["slow"])
        message = _diagnostic(caplog, "render was slow:")
        assert "gpu 50 %, 1000/16000 MB" in message
        assert "chars)" in message
        assert speaker.health()["slow_renders"] == 1
    finally:
        speaker.close()


def test_fast_render_is_not_reported(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.wav_speaker as wav_speaker_module

    monkeypatch.setattr(wav_speaker_module, "gpu_status", lambda: None)
    speaker = WavSpeaker(FakeRenderer(), FakePlayer())
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("fast", text="line"))
        _wait_for(lambda: spoken == ["fast"])
        time.sleep(0.1)
        assert not any("render was slow" in message for message in _messages(caplog))
        assert speaker.health()["slow_renders"] == 0
    finally:
        speaker.close()


def test_render_timeout_reports_duration_with_and_without_gpu(
    monkeypatch, caplog, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.wav_speaker as wav_speaker_module

    monkeypatch.setattr(wav_speaker_module, "gpu_status", lambda: None)
    renderer = ProvisionalFake()
    renderer.block_next()
    speaker = WavSpeaker(renderer, FakePlayer(), cache_dir=tmp_path, render_timeout_s=0.05)
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("slow", text="line"))
        _wait_for(lambda: spoken == ["slow"])
        message = _diagnostic(caplog, "render timed out after")
        assert message.endswith("(4 chars)")
        monkeypatch.setattr(wav_speaker_module, "gpu_status", lambda: "gpu 98 %, 1/2 MB")
        speaker._report_render("timed out after", 4.0, "line")
        _wait_for(
            lambda: any(
                message.endswith("chars), gpu 98 %, 1/2 MB") for message in _messages(caplog)
            )
        )
    finally:
        renderer.release.set()
        speaker.close()


def test_render_failure_reports_duration(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import pitwall.audio.wav_speaker as wav_speaker_module

    monkeypatch.setattr(wav_speaker_module, "gpu_status", lambda: None)
    speaker = WavSpeaker(FakeRenderer(fail_text="bad"), FakePlayer())
    dropped: list[str] = []
    speaker.on_dropped = dropped.append
    try:
        speaker.speak(make_call("bad", text="bad"))
        _wait_for(lambda: dropped == ["bad"])
        assert "chars)" in _diagnostic(caplog, "render failed after")
    finally:
        speaker.close()


def test_render_timeout_without_provisional_drops_call() -> None:
    renderer = FakeRenderer()
    renderer.block_next()
    speaker = WavSpeaker(renderer, FakePlayer(), render_timeout_s=0.05)
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("slow", text="slow"))
        _wait_for(lambda: dropped == ["slow"])
        renderer.release.set()
        speaker.speak(make_call("next", text="next"))
        _wait_for(lambda: spoken == ["next"])
        assert dropped == ["slow"]
        assert speaker.health()["dropped"] == 1
    finally:
        renderer.release.set()
        speaker.close()


def test_stuck_renders_are_capped_and_not_redispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pitwall.audio.wav_speaker.BREAKER_OPEN_S", 0.2)
    gate = threading.Event()

    class StuckRenderer(FakeRenderer):
        def __init__(self) -> None:
            super().__init__()
            self.stuck_calls = 0

        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if text.startswith("cold") and not background:
                self.stuck_calls += 1
                gate.wait(5.0)
            return super().render(text, priority, background=background)

    def lane_threads() -> int:
        return sum(thread.name == "wav-render-lane" for thread in threading.enumerate())

    renderer = StuckRenderer()
    speaker = WavSpeaker(renderer, FakePlayer(), render_timeout_s=0.05)
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        for index in range(3):
            speaker.speak(make_call(f"cold{index}", text=f"cold{index}"))
        _wait_for(lambda: len(dropped) == 3)
        assert renderer.stuck_calls == 3
        _wait_for(lambda: not speaker.health()["breaker_open"])

        threads_before = lane_threads()
        speaker.speak(make_call("cold3", text="cold3"))
        _wait_for(lambda: len(dropped) == 4)
        assert renderer.stuck_calls == 3
        assert lane_threads() == threads_before

        gate.set()
        _wait_for(lambda: not speaker._late)
        speaker.speak(make_call("cold4", text="cold4"))
        _wait_for(lambda: spoken == ["cold4"])
        assert ("cold4", 2, False) in renderer.calls
    finally:
        gate.set()
        speaker.close()


def test_failed_late_render_still_queues_background_fill() -> None:
    gate = threading.Event()

    class LateFailRenderer(FakeRenderer):
        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if text == "late" and not background:
                self.calls.append((text, priority, background))
                gate.wait(5.0)
                raise RuntimeError("late failure")
            return super().render(text, priority, background=background)

    renderer = LateFailRenderer()
    speaker = WavSpeaker(renderer, FakePlayer(), render_timeout_s=0.05)
    dropped: list[str] = []
    speaker.on_dropped = dropped.append
    try:
        speaker.speak(make_call("late", text="late"))
        _wait_for(lambda: dropped == ["late"])
        gate.set()
        _wait_for(lambda: ("late", 2, True) in renderer.calls)
        _wait_for(lambda: speaker._cached(speaker._key("late", 2)) is not None)
    finally:
        gate.set()
        speaker.close()


def test_breaker_opens_after_repeated_failures_and_probe_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pitwall.audio.wav_speaker.BREAKER_OPEN_S", 0.5)
    renderer = FakeRenderer(fail_text="bad")
    speaker = WavSpeaker(renderer, FakePlayer())
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        for index in range(3):
            speaker.speak(make_call(f"bad-{index}", text="bad"))
        _wait_for(lambda: len(dropped) == 3)
        assert speaker.health()["breaker_open"] is True
        speaker.speak(make_call("skipped", text="bad"))
        _wait_for(lambda: len(dropped) == 4)
        assert [call for call in renderer.calls if not call[2]] == [("bad", 2, False)] * 3
        assert speaker.health()["render_failures"] == 3
        _wait_for(lambda: not speaker.health()["breaker_open"])
        speaker.speak(make_call("probe", text="good"))
        _wait_for(lambda: spoken == ["probe"])
        assert renderer.calls[-1] == ("good", 2, False)
        assert speaker.health()["breaker_open"] is False
    finally:
        speaker.close()


def test_failing_provisional_drops_call_and_worker_keeps_playing() -> None:
    renderer = ProvisionalFake(fail_text="bad", provisional_error="provisional broke")
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("bad", text="bad"))
        _wait_for(lambda: dropped == ["bad"])
        speaker.speak(make_call("good", text="good"))
        _wait_for(lambda: spoken == ["good"])
        assert speaker.health()["worker_alive"] is True
        assert len(player.played) == 1
    finally:
        speaker.close()


def test_stuck_provisional_audio_times_out_and_is_not_redispatched() -> None:
    class BlockingProvisional(ProvisionalFake):
        def __init__(self) -> None:
            super().__init__(fail_text="bad")
            self.gate = threading.Event()
            self.provisional_calls = 0

        def provisional(self, text: str, priority: int) -> tuple[bytes, float]:
            self.provisional_calls += 1
            self.gate.wait(5.0)
            return super().provisional(text, priority)

    renderer = BlockingProvisional()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player, render_timeout_s=0.05)
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    try:
        speaker.speak(make_call("first", text="bad"))
        _wait_for(lambda: dropped == ["first"])
        speaker.speak(make_call("second", text="bad"))
        _wait_for(lambda: dropped == ["first", "second"])
        assert renderer.provisional_calls == 1
        renderer.gate.set()
        _wait_for(lambda: speaker._provisional_job is not None and speaker._provisional_job.done())
        speaker.speak(make_call("third", text="bad"))
        _wait_for(lambda: spoken == ["third"])
        assert player.played == [renderer.provisional_wav]
    finally:
        renderer.gate.set()
        speaker.close()


def test_open_breaker_still_fills_cache_in_background() -> None:
    class ForegroundDownRenderer(ProvisionalFake):
        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if not background:
                raise RuntimeError("foreground down")
            return super().render(text, priority, background=background)

    renderer = ForegroundDownRenderer()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    spoken: list[str] = []
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    key = speaker._key("t3", 2)
    try:
        for index in range(3):
            speaker.speak(make_call(f"t{index}", text=f"t{index}"))
        _wait_for(lambda: speaker.health()["provisional_plays"] == 3)
        assert speaker.health()["breaker_open"] is True
        speaker.speak(make_call("t3", text="t3"))
        _wait_for(lambda: speaker._cached(key) is not None)
        assert ("t3", 2, False) not in renderer.calls
        assert renderer.calls.count(("t3", 2, True)) == 1
        speaker.speak(make_call("t3-again", text="t3"))
        _wait_for(lambda: spoken[-1:] == ["t3-again"])
        cached_wav = speaker._cached(key)[0]
        assert player.played[-1] == cached_wav
        assert cached_wav != renderer.provisional_wav
    finally:
        speaker.close()


def test_open_breaker_fills_cache_without_provisional_audio() -> None:
    class ForegroundDownRenderer(FakeRenderer):
        def render(
            self, text: str, priority: int, *, background: bool = False
        ) -> tuple[bytes, float]:
            if text.startswith("down") and not background:
                raise RuntimeError("foreground down")
            return super().render(text, priority, background=background)

    renderer = ForegroundDownRenderer()
    player = FakePlayer()
    speaker = WavSpeaker(renderer, player)
    dropped: list[str] = []
    spoken: list[str] = []
    speaker.on_dropped = dropped.append
    speaker.on_spoken = lambda call_id, _time: spoken.append(call_id)
    key = speaker._key("line", 2)
    try:
        for index in range(3):
            speaker.speak(make_call(f"down{index}", text=f"down{index}"))
        _wait_for(lambda: len(dropped) == 3)
        assert speaker.health()["breaker_open"] is True
        speaker.speak(make_call("line", text="line"))
        _wait_for(lambda: dropped == ["down0", "down1", "down2", "line"])
        _wait_for(lambda: speaker._cached(key) is not None)
        assert renderer.calls.count(("line", 2, True)) == 1
        speaker.speak(make_call("line-again", text="line"))
        _wait_for(lambda: spoken == ["line-again"])
        assert player.played == [speaker._cached(key)[0]]
    finally:
        speaker.close()


def test_health_reports_render_and_playback_state() -> None:
    renderer = ProvisionalFake(fail_text="bad")
    speaker = WavSpeaker(renderer, FakePlayer())
    try:
        speaker.speak(make_call("bad", text="bad"))
        _wait_for(lambda: speaker.health()["provisional_plays"] == 1)
        health = speaker.health()
        assert set(health) == {
            "engine",
            "worker_alive",
            "breaker_open",
            "render_failures",
            "render_timeouts",
            "slow_renders",
            "provisional_plays",
            "dropped",
            "last_error",
            "last_error_age_s",
        }
        assert health["engine"] == "fake"
        assert health["worker_alive"] is True
        assert health["breaker_open"] is False
        assert health["render_failures"] == 1
        assert health["render_timeouts"] == 0
        assert health["dropped"] == 0
        assert health["last_error"] == "render failed"
        assert health["last_error_age_s"] >= 0
    finally:
        speaker.close()


def test_wav_speaker_contract_with_render_timeout() -> None:
    player = FakePlayer()
    speaker = WavSpeaker(FakeRenderer(), player, render_timeout_s=1.0)
    try:
        check_speaker_contract(speaker, player)
    finally:
        speaker.close()
