from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import cast

import numpy as np

from pitwall.audio.piper_tts import (
    make_piper_tone_synths,
)
from pitwall.audio.wav_speaker import (
    InvalidWav,
    Player,
    Synth,
    WavSpeaker,
    default_player,
    radio_blip,
    to_wav,
    wav_info,
)
from pitwall.audio.wav_speaker import _read_cache as _read_cache
from pitwall.audio.wav_speaker import _write_cache as _write_cache
from pitwall.config.models import SpeechSettings

log = logging.getLogger(__name__)

BACKGROUND_TIMEOUT_S = 120.0


class OmniVoiceError(RuntimeError):
    pass


def _error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode(errors="replace").strip()
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                message = value.get("message")
                if isinstance(message, str) and message:
                    return message
    return body.decode(errors="replace").strip()


def _request(url: str, *, data: bytes | None, timeout_s: float) -> bytes:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return cast(bytes, response.read())
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read())
        message = f"HTTP {exc.code}"
        if detail:
            message += f": {detail}"
        raise OmniVoiceError(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OmniVoiceError(f"request to {url} failed: {exc}") from exc


def _post_json(url: str, payload: dict[str, object], timeout_s: float) -> bytes:
    data = json.dumps(payload).encode()
    return _request(url, data=data, timeout_s=timeout_s)


def make_omnivoice_synth(settings: SpeechSettings, timeout_s: float | None = None) -> Synth:
    timeout = settings.omnivoice_timeout_s if timeout_s is None else timeout_s
    endpoint = settings.omnivoice_url.rstrip("/") + "/v1/audio/speech"

    def synth(text: str) -> tuple[bytes, float]:
        response = _post_json(
            endpoint,
            {
                "input": text,
                "voice": settings.omnivoice_voice,
                "language": settings.omnivoice_language,
                "response_format": "wav",
                "seed": settings.omnivoice_seed,
            },
            timeout,
        )
        try:
            seconds = wav_info(response)
            with wave.open(io.BytesIO(response), "rb") as reader:
                sample_rate = reader.getframerate()
                if settings.radio_click:
                    frames = reader.getnframes()
                    audio = np.frombuffer(reader.readframes(frames), dtype="<i2").astype(np.float32)
                    samples = np.concatenate((radio_blip(sample_rate), audio / 32767.0))
                    response = to_wav(samples, sample_rate)
                    seconds = wav_info(response)
        except (InvalidWav, wave.Error, EOFError, ZeroDivisionError) as exc:
            raise OmniVoiceError(f"OmniVoice returned invalid WAV audio: {exc}") from exc
        return response, seconds

    return synth


def register_voice(settings: SpeechSettings) -> str:
    if not settings.omnivoice_voice.strip():
        raise OmniVoiceError("speech.omnivoice_voice must be set")
    if not settings.omnivoice_ref_wav.strip():
        raise OmniVoiceError("speech.omnivoice_ref_wav must point to a reference WAV")
    if not settings.omnivoice_ref_text.strip():
        raise OmniVoiceError("speech.omnivoice_ref_text must point to a transcript text file")

    wav_path = Path(settings.omnivoice_ref_wav).expanduser()
    text_path = Path(settings.omnivoice_ref_text).expanduser()
    if not wav_path.is_file():
        raise OmniVoiceError(f"reference WAV not found: {wav_path}")
    if not text_path.is_file():
        raise OmniVoiceError(f"reference transcript not found: {text_path}")
    try:
        wav_data = wav_path.read_bytes()
        transcript = text_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OmniVoiceError(f"could not read OmniVoice reference files: {exc}") from exc
    if not transcript:
        raise OmniVoiceError(f"reference transcript is empty: {text_path}")

    _post_json(
        settings.omnivoice_url.rstrip("/") + "/v1/audio/voices",
        {
            "name": settings.omnivoice_voice,
            "ref_text": transcript,
            "wav_b64": base64.b64encode(wav_data).decode("ascii"),
        },
        settings.omnivoice_timeout_s,
    )
    return hashlib.sha256(wav_data + transcript.encode()).hexdigest()


def fetch_model_id(settings: SpeechSettings) -> str:
    endpoint = settings.omnivoice_url.rstrip("/") + "/v1/models"
    response = _request(endpoint, data=None, timeout_s=settings.omnivoice_timeout_s)
    try:
        payload = json.loads(response)
        model_id = payload["data"][0]["id"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError, TypeError) as exc:
        raise OmniVoiceError("OmniVoice returned no model id from /v1/models") from exc
    if not isinstance(model_id, str) or not model_id:
        raise OmniVoiceError("OmniVoice returned an invalid model id from /v1/models")
    return model_id


def omnivoice_cache_key(settings: SpeechSettings, model_id: str, ref_identity: str) -> str:
    payload = {
        "model_id": model_id,
        "omnivoice_voice": settings.omnivoice_voice,
        "ref_identity": ref_identity,
        "seed": settings.omnivoice_seed,
        "language": settings.omnivoice_language,
        "response_format": "wav",
        "omnivoice_voice_version": settings.omnivoice_voice_version,
        "radio_click": settings.radio_click,
    }
    encoded = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class OmniVoiceRenderer:
    per_priority = False

    def __init__(
        self,
        settings: SpeechSettings,
        model_id: str,
        ref_identity: str,
        fallback_tones: dict[int, Synth] | None,
    ) -> None:
        self.label = f"omnivoice ({settings.omnivoice_voice})"
        self.cache_identity = omnivoice_cache_key(settings, model_id, ref_identity)
        self.fallback_tones = fallback_tones
        self._synth = make_omnivoice_synth(settings)
        self._background_synth = make_omnivoice_synth(settings, timeout_s=BACKGROUND_TIMEOUT_S)

    def render(self, text: str, priority: int, *, background: bool = False) -> tuple[bytes, float]:
        synth = self._background_synth if background else self._synth
        return synth(text)

    def provisional(self, text: str, priority: int) -> tuple[bytes, float] | None:
        if self.fallback_tones is None:
            return None
        return self.fallback_tones.get(priority, self.fallback_tones[2])(text)


def make_omnivoice_speaker(settings: SpeechSettings, player: Player | None = None) -> WavSpeaker:
    model_id = fetch_model_id(settings)
    ref_identity = register_voice(settings)
    try:
        fallback_tones = make_piper_tone_synths(settings)
    except Exception as exc:
        log.warning("Piper tone fallback unavailable (%s)", exc)
        fallback_tones = None
    fallback = None
    if fallback_tones is None:
        from pitwall.audio.speaker import make_speaker

        fallback = make_speaker(
            settings.model_copy(update={"engine": "sapi" if sys.platform == "win32" else "null"})
        )
    renderer = OmniVoiceRenderer(settings, model_id, ref_identity, fallback_tones)
    return WavSpeaker(
        renderer,
        player or default_player(),
        cache_dir=Path(settings.voices_dir) / ".phrases",
        fallback=fallback,
    )
