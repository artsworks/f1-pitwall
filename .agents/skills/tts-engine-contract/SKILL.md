---
name: tts-engine-contract
description: Add or change a Pitwall speech engine (TTS) without breaking dispatcher lifecycle, dashboard telemetry, local playback, browser audio streaming or replay.
---

# Speech engine contract

Read this skill before you change `src/pitwall/audio/`, speaker wiring in `src/pitwall/cli/serve.py` or `src/pitwall/cli/recordings.py`, or `Hub.audio`. The full contract is in `docs/04-audio-ui.md` under "Speech engine contract". This skill lists the rules and the required tests.

## Input

The dispatcher calls `speak(call)` with the whole dispatcher `Call`.

- Use `call.id` in every callback. The hub and the web frames key on it.
- Order by `call.priority`. A P1 call stops the call that is playing.
- Drop the call when `call.handoff_age_s` plus the time in the engine queue passes `call.max_age_s`. `max_age_s` of `None` means no limit.
- Read `deadline_ms`, `tags`, `lap`, `urgency` and `rank` only for logs or extensions.
- Do not check `screen_only` or `still_true`. The dispatcher owns them. It does not send `screen_only` calls to an engine with `speaks_audio` true.

## Callbacks

`wire_speaker(speaker, hub, dispatcher)` in `speaker.py` connects them. Do not wire them by hand.

| Callback | When the engine calls it | Wired to |
|---|---|---|
| `on_spoken(call_id, wall_time)` | Local playback starts | `hub.spoken` and `Metrics.note_spoken` |
| `on_audio(call_id, priority, wav_bytes)` | After playback starts, with the complete WAV | `hub.audio` |
| `on_dropped(call_id)` | The engine drops the call (age limit, or a render failure with no fallback) | `dispatcher.note_dropped` and `hub.cancel` |

- Call each callback at most once per call.
- A dropped call gets `on_dropped` and no `on_spoken`.
- After `cancel(call_id)`, send no `on_audio` for that call.
- After `flush()`, send no callback for any call received before the flush.
- Call `on_spoken` at playback start, not at render end. `tts_ms` measures dispatcher handoff to playback start.

## WAV format

Every `on_audio` payload is one complete WAV file:

- RIFF and WAVE headers
- 1 channel
- 16-bit PCM samples
- sample rate above 0
- at least one frame

`wav_info()` in `wav_speaker.py` checks these and raises `InvalidWav`.

## Capability flags

| Member | Meaning |
|---|---|
| `speaks_audio` | The engine plays audio. The dispatcher does not send `screen_only` calls to it. |
| `supports_audio_stream` | The engine produces WAV bytes and has `on_audio`. Only then does `hub.streams_audio` become true. |
| `warm(phrases)` | `pitwall voice warm` can fill the phrase cache. |
| `flush()` | Replay seek and reset can drop queued audio. |
| `queue_status()` | The engine reports its queued count and oldest age. |

Do not set `supports_audio_stream` on an engine that cannot produce bytes. SAPI plays audio and reports playback, but it has no WAV, so it does not stream.

## Translation layer

Build WAV engines on `WavSpeaker` in `wav_speaker.py`. `WavSpeaker` owns the queue, P1 preemption, cancellation, age drops, the phrase cache, the player, flush and every callback. The vendor code is a `WavRenderer`:

- `label`: the speaker name for health and the startup banner.
- `cache_identity`: a string that changes whenever the audio for the same text changes (model, voice, seed, speed).
- `per_priority`: `True` if the audio depends on the call priority.
- `render(text, priority, *, background=False)`: returns `(wav_bytes, seconds)`. Raise on failure.
- `provisional(text, priority)` (optional): audio to play now while `render(..., background=True)` fills the cache.

If the vendor returns PCM or chunks, collect the chunks in order and call `pcm_to_wav(chunks, sample_rate, channels=..., dtype=...)`. Convert MP3 or Opus to PCM first. If a cancel or flush happens during rendering, `WavSpeaker` discards the result.

If an engine plays audio itself and cannot report playback start, it is not a compliant speaker. Put a renderer in front of it that returns WAV bytes.

## Do not

- Do not add `isinstance` checks on engine classes or tests on engine names in live, replay or server code. Use the capability flags and `wire_speaker()`.
- Do not change `Metrics.note_handoff`, `Metrics.note_spoken`, `note_queue_wait`, `queue_status`, `packet_to_snapshot_ms` or `packet_to_ws_ms`.
- Do not rename the dashboard `latency` keys `queue_count`, `queue_oldest_s`, `queue_wait_p99_ms`, `tts_p99_ms` and `packet_to_ws_p99_ms`.

## Add an engine

1. Write a renderer in `src/pitwall/audio/<engine>_tts.py`.
2. Write a factory that returns a `WavSpeaker`. The factory raises if the engine is not available.
3. Register the engine id. Built-in engines go in `ENGINES` in `speaker.py`. Plugins call `register_engine()`.

```python
from pathlib import Path

from pitwall.audio.speaker import EngineSpec, register_engine
from pitwall.audio.wav_speaker import WavSpeaker, default_player, pcm_to_wav


class AcmeRenderer:
    label = "acme"
    per_priority = False

    def __init__(self, client, voice: str) -> None:
        self._client = client
        self.cache_identity = f"acme:{voice}"

    def render(self, text: str, priority: int, *, background: bool = False):
        chunks = self._client.synthesize(text)  # float32 mono, 24 kHz
        return pcm_to_wav(chunks, 24000, dtype="float32")


def make_acme_speaker(settings):
    renderer = AcmeRenderer(AcmeClient(), settings.voice or "default")
    return WavSpeaker(renderer, default_player(), cache_dir=Path(settings.voices_dir) / ".phrases")


register_engine(EngineSpec(id="acme", factory=make_acme_speaker, fallback="piper"))
```

Then set `speech.engine: acme`. If the factory raises, `make_speaker` prints a warning and tries the fallback id.

## Required tests

Generic tests use `tests/audio_contract.py`. They must not import Piper or OmniVoice.

- Run `check_speaker_contract()` on the new speaker with the vendor network or model mocked. It checks the `Call` input, the WAV format, the `on_audio` id and priority, `on_spoken` after playback start, no callbacks after cancel or flush, and `on_dropped` for age drops.
- If the vendor returns PCM or chunks, test that the stream WAV is mono 16-bit and keeps the chunk order.
- Test that a render failure goes to the fallback speaker or to `on_dropped`.

Vendor tests go in `tests/test_<engine>.py`:

- the request shape sent to the vendor
- the cache identity changes when the voice, model or settings change
- availability: a factory error falls back to the next engine
- vendor-only features such as voice download, registration or warm

Run these before you push:

```bash
uv run pytest -q tests/test_wav_speaker.py tests/test_speaker_contract.py tests/test_<engine>.py tests/test_review.py tests/test_cli_start.py
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```
