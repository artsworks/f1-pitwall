from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from pitwall.audio.piper_tts import to_wav
from pitwall.audio.splice import decode_wav, splice, trim_silence

RATE = 24_000


def _tone(hz: float, seconds: float, rate: int = RATE) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return to_wav((0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32), rate)


def _frames(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        return reader.getnframes()


def _trimmed_len(wav: bytes) -> int:
    samples, _ = decode_wav(wav)
    return len(trim_silence(samples))


def test_splice_length_no_gap() -> None:
    wav_a, wav_b = _tone(440, 0.2), _tone(880, 0.3)
    fade_n = round(RATE * 10 / 1000)
    expected = _trimmed_len(wav_a) + _trimmed_len(wav_b) - fade_n
    assert _frames(splice([wav_a, wav_b], gap_ms=0, fade_ms=10)) == expected


def test_splice_length_with_gap() -> None:
    wav_a, wav_b = _tone(440, 0.2), _tone(880, 0.3)
    fade_n = round(RATE * 10 / 1000)
    gap_n = round(RATE * 80 / 1000)
    expected = _trimmed_len(wav_a) + gap_n + _trimmed_len(wav_b) - 2 * fade_n
    assert _frames(splice([wav_a, wav_b], gap_ms=80, fade_ms=10)) == expected


def test_splice_rejects_rate_mismatch() -> None:
    with pytest.raises(ValueError, match="sample rates differ"):
        splice([_tone(440, 0.1, 24_000), _tone(440, 0.1, 22_050)])


def test_trim_strips_edges_keeps_inner_gap() -> None:
    t = np.arange(240) / RATE
    tone = 0.5 * np.sin(2 * np.pi * 440 * t)
    samples = np.concatenate([np.zeros(100), tone, np.zeros(80), tone, np.zeros(100)]).astype(
        np.float32
    )
    trimmed = trim_silence(samples)
    assert trimmed[0] != 0
    assert trimmed[-1] != 0
    assert np.sum(trimmed == 0) >= 80
    assert len(trimmed) <= 560
    assert trim_silence(np.zeros(50, dtype=np.float32)).size == 0


def test_decode_wav_rejects_stereo() -> None:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(np.zeros(200, dtype="<i2").tobytes())
    with pytest.raises(ValueError, match="one channel"):
        decode_wav(buf.getvalue())
