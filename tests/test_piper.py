import io
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from pitwall.audio.dispatcher import Call
from pitwall.audio.piper_tts import PiperSpeaker, make_piper_synth, radio_blip, to_wav
from pitwall.audio.speaker import NullSpeaker, make_speaker
from pitwall.config.models import SpeechSettings


def _call(cid: str, priority: int = 2, text: str = "box box") -> Call:
    t = time.monotonic()
    return Call(
        id=cid,
        rule_id=cid,
        priority=priority,
        text=text,
        tags=[],
        deadline_ms=5000,
        lap=1,
        t=t,
        trigger_t=t,
    )


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.stops = 0

    def play(self, wav: bytes) -> None:
        self.played.append(wav)

    def stop(self) -> None:
        self.stops += 1


def _speaker(seconds: float) -> tuple[PiperSpeaker, FakePlayer, list[str], list[str]]:
    player = FakePlayer()
    synthed: list[str] = []

    def synth(text: str) -> tuple[bytes, float]:
        synthed.append(text)
        return text.encode(), seconds

    sp = PiperSpeaker(synth, player)
    spoken: list[str] = []
    sp.on_spoken = lambda cid, t: spoken.append(cid)
    return sp, player, spoken, synthed


def _wait_for(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def test_piper_speaker_plays_reports_and_caches() -> None:
    sp, player, spoken, synthed = _speaker(0.0)
    sp.speak(_call("a", text="front left cold"))
    sp.speak(_call("b", text="front left cold"))
    _wait_for(lambda: spoken == ["a", "b"])
    assert player.played == [b"front left cold", b"front left cold"]
    assert synthed == ["front left cold"]
    sp.close()


def test_piper_speaker_skips_cancelled() -> None:
    sp, player, spoken, _ = _speaker(0.3)
    sp.speak(_call("a"))
    _wait_for(lambda: spoken == ["a"])
    sp.speak(_call("b"))
    sp.cancel("b")
    sp.speak(_call("c", text="c"))
    _wait_for(lambda: spoken == ["a", "c"])
    sp.close()


def test_piper_p1_preempts_and_jumps_queue() -> None:
    sp, player, spoken, _ = _speaker(5.0)
    sp.speak(_call("long", priority=3, text="long"))
    _wait_for(lambda: spoken == ["long"])
    sp.speak(_call("p3", priority=3, text="p3"))
    t0 = time.monotonic()
    sp.speak(_call("urgent", priority=1, text="urgent"))
    _wait_for(lambda: spoken[:2] == ["long", "urgent"])
    assert time.monotonic() - t0 < 1.0
    assert player.stops >= 1
    sp.close()


def test_to_wav_roundtrip() -> None:
    data = to_wav(np.zeros(2205), 22050)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (
            22050,
            1,
            2,
            2205,
        )


def test_radio_blip_shape() -> None:
    sample_rate = 22050
    pip1_samples = int(sample_rate * 0.06)
    pip_gap_samples = int(sample_rate * 0.03)
    pip2_samples = int(sample_rate * 0.07)
    trailing_gap_samples = int(sample_rate * 0.06)
    pip2_start = pip1_samples + pip_gap_samples
    trailing_gap_start = pip2_start + pip2_samples
    blip = radio_blip(sample_rate)

    assert len(blip) == pip1_samples + pip_gap_samples + pip2_samples + trailing_gap_samples
    assert 0.1 < np.abs(blip).max() < 0.2
    assert abs(blip[0]) < 1e-3
    assert abs(blip[pip1_samples - 1]) < 1e-3
    assert abs(blip[pip2_start]) < 1e-3
    assert abs(blip[trailing_gap_start - 1]) < 1e-3
    assert np.all(blip[pip1_samples:pip2_start] == 0)
    assert np.all(blip[trailing_gap_start:] == 0)

    magnitude = np.abs(np.fft.rfft(blip))
    frequencies = np.fft.rfftfreq(len(blip), 1 / sample_rate)
    assert np.sum(magnitude * frequencies) / np.sum(magnitude) < 1000


def test_missing_voice_falls_back(tmp_path: Path) -> None:
    s = SpeechSettings(engine="piper", voices_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError, match="pitwall voices get"):
        make_piper_synth(s)
    assert isinstance(make_speaker(s), NullSpeaker)


VOICES = Path.home() / "voices"


@pytest.mark.skipif(not (VOICES / "en_GB-alan-medium.onnx").exists(), reason="no voice")
def test_real_piper_synth_renders_speech() -> None:
    synth = make_piper_synth(SpeechSettings(voices_dir=str(VOICES)))
    wav, seconds = synth("Box this lap.")
    assert 0.5 < seconds < 5.0
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnframes() / w.getframerate() == pytest.approx(seconds)
