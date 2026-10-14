"""Trim and splice WAV segments for stem+slot voice auditions."""

from __future__ import annotations

import io
import wave
from collections.abc import Sequence

import numpy as np

from pitwall.audio.piper_tts import to_wav


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a mono 16-bit WAV into float32 samples and its sample rate."""
    with wave.open(io.BytesIO(data), "rb") as reader:
        if reader.getnchannels() != 1:
            raise ValueError("WAV must have one channel")
        if reader.getsampwidth() != 2:
            raise ValueError("WAV must use 16-bit samples")
        rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0, rate


def trim_silence(samples: np.ndarray, threshold_db: float = -45.0) -> np.ndarray:
    """Drop leading and trailing samples below `threshold_db` (dBFS)."""
    threshold = 10 ** (threshold_db / 20)
    loud = np.flatnonzero(np.abs(samples) >= threshold)
    if loud.size == 0:
        return samples[:0]
    return samples[loud[0] : loud[-1] + 1]


def splice(
    wavs: Sequence[bytes],
    *,
    gap_ms: int = 0,
    fade_ms: int = 10,
    threshold_db: float = -45.0,
) -> bytes:
    """Join trimmed WAV segments left to right with optional gaps and
    a linear crossfade at every join."""
    if not wavs:
        raise ValueError("no segments to splice")
    decoded = [decode_wav(wav) for wav in wavs]
    rates = {rate for _, rate in decoded}
    if len(rates) > 1:
        raise ValueError(f"sample rates differ: {sorted(rates)}")
    rate = decoded[0][1]
    segments = [trim_silence(samples, threshold_db) for samples, _ in decoded]

    parts: list[np.ndarray] = []
    gap = np.zeros(round(rate * gap_ms / 1000), dtype=np.float32)
    for index, segment in enumerate(segments):
        if index and gap_ms > 0:
            parts.append(gap)
        parts.append(segment)

    fade_n = round(rate * fade_ms / 1000)
    out = parts[0]
    for part in parts[1:]:
        n = min(fade_n, len(out), len(part))
        if n == 0:
            out = np.concatenate([out, part])
        else:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            out = np.concatenate([out[:-n], out[-n:] * (1 - ramp) + part[:n] * ramp, part[n:]])
    return to_wav(out, rate)
