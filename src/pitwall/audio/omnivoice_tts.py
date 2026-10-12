from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import queue
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import cast

import numpy as np

from pitwall.audio.piper_tts import (
    CACHE_SIZE,
    PiperSpeaker,
    Player,
    Synth,
    default_player,
    make_piper_tone_synths,
    radio_blip,
    to_wav,
)
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
            with wave.open(io.BytesIO(response), "rb") as reader:
                sample_rate = reader.getframerate()
                frames = reader.getnframes()
                seconds = frames / sample_rate
                if settings.radio_click:
                    if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                        raise OmniVoiceError("OmniVoice returned WAV audio that is not s16 mono")
                    audio = np.frombuffer(reader.readframes(frames), dtype="<i2").astype(np.float32)
                    samples = np.concatenate((radio_blip(sample_rate), audio / 32767.0))
                    response = to_wav(samples, sample_rate)
                    seconds = len(samples) / sample_rate
        except (wave.Error, EOFError, ZeroDivisionError) as exc:
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


class OmniVoiceSpeaker(PiperSpeaker):
    def __init__(
        self,
        omnivoice: Synth,
        fallback: dict[int, Synth] | None,
        player: Player,
        label: str,
        cache_dir: Path,
        cache_key: str,
    ) -> None:
        super().__init__(
            fallback[2] if fallback else omnivoice,
            player,
            label=label,
            tones=fallback or {},
            cache_dir=cache_dir,
            cache_key=cache_key,
        )
        self._omnivoice = omnivoice
        self._background_synth = omnivoice
        self._cache_lock = threading.Lock()
        self._pending: set[str] = set()
        self._background_queue: queue.Queue[str | None] = queue.Queue()
        self._background_thread = threading.Thread(
            target=self._background_worker, daemon=True, name="omnivoice"
        )
        self._background_thread.start()

    def _store_cache(self, key: tuple[int, str], result: tuple[bytes, float]) -> None:
        self._cache[key] = result
        self._cache.move_to_end(key)
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def _queue_background(self, text: str) -> None:
        if text not in self._pending:
            self._pending.add(text)
            self._background_queue.put(text)

    def render(self, text: str, priority: int = 2) -> tuple[bytes, float]:
        key = (0, text)
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            path = self._phrase_path(key)
            if path is not None and path.is_file():
                wav = path.read_bytes()
                with wave.open(io.BytesIO(wav), "rb") as reader:
                    result = wav, reader.getnframes() / reader.getframerate()
                self._store_cache(key, result)
                return result
            if self._tones:
                self._queue_background(text)
                return self._tones.get(priority, self._synth)(text)

            result = self._omnivoice(text)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(result[0])
            self._store_cache(key, result)
            return result

    def warm(self, phrases: list[tuple[str, int]]) -> int:
        texts = list(dict.fromkeys(text for text, _ in phrases))
        if self._cache_dir is None:
            return len(texts)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(texts, start=1):
            print(f"warming {index}/{len(texts)}: {text}")
            key = (0, text)
            path = self._phrase_path(key)
            if path is not None and path.is_file():
                continue
            result = self._background_synth(text)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(result[0])
            with self._cache_lock:
                self._store_cache(key, result)
        return len(texts)

    def _background_worker(self) -> None:
        while True:
            text = self._background_queue.get()
            if text is None:
                return
            key = (0, text)
            try:
                result = self._background_synth(text)
                path = self._phrase_path(key)
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(".wav.tmp")
                    temporary.write_bytes(result[0])
                    temporary.replace(path)
                with self._cache_lock:
                    self._store_cache(key, result)
            except Exception as exc:
                log.warning("OmniVoice background synthesis failed for %r: %s", text, exc)
            finally:
                with self._cache_lock:
                    self._pending.discard(text)

    def close(self) -> None:
        super().close()
        self._background_queue.put(None)
        self._background_thread.join(timeout=1.0)


def make_omnivoice_speaker(
    settings: SpeechSettings, player: Player | None = None
) -> OmniVoiceSpeaker:
    model_id = fetch_model_id(settings)
    ref_identity = register_voice(settings)
    try:
        fallback = make_piper_tone_synths(settings)
    except Exception as exc:
        log.warning("Piper tone fallback unavailable (%s)", exc)
        fallback = None
    speaker = OmniVoiceSpeaker(
        make_omnivoice_synth(settings),
        fallback,
        player or default_player(),
        label=f"omnivoice ({settings.omnivoice_voice})",
        cache_dir=Path(settings.voices_dir) / ".phrases",
        cache_key=omnivoice_cache_key(settings, model_id, ref_identity),
    )
    speaker._background_synth = make_omnivoice_synth(settings, timeout_s=BACKGROUND_TIMEOUT_S)
    return speaker
