"""Piper WAV renderer and voice management.

Voices are downloaded once into `speech.voices_dir` (`pitwall voice get`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import yaml

from pitwall.audio.wav_speaker import (
    CACHE_SIZE as CACHE_SIZE,
)
from pitwall.audio.wav_speaker import (
    NullPlayer as NullPlayer,
)
from pitwall.audio.wav_speaker import (
    Player,
    Synth,
    WavSpeaker,
)
from pitwall.audio.wav_speaker import (
    WinsoundPlayer as WinsoundPlayer,
)
from pitwall.audio.wav_speaker import (
    default_player as default_player,
)
from pitwall.audio.wav_speaker import (
    radio_blip as radio_blip,
)
from pitwall.audio.wav_speaker import (
    to_wav as to_wav,
)
from pitwall.config.models import RuleDefModel, SpeechSettings

SUGGESTED_VOICES = (
    "en_GB-northern_english_male-medium",
    "en_GB-alan-medium",
    "en_US-ryan-high",
    "en_US-lessac-medium",
)
PIPER_NOISE_SCALE = 0.667  # Piper's default phoneme noise; scaled per tone


def voice_path(settings: SpeechSettings, name: str | None = None) -> Path:
    return Path(settings.voices_dir) / f"{name or settings.piper_voice}.onnx"


def installed_voices(settings: SpeechSettings) -> list[str]:
    d = Path(settings.voices_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.onnx") if p.with_suffix(".onnx.json").exists())


def download_voice(settings: SpeechSettings, name: str) -> Path:
    from piper.download_voices import download_voice as _download

    d = Path(settings.voices_dir)
    d.mkdir(parents=True, exist_ok=True)
    _download(name, d)
    return voice_path(settings, name)


def make_piper_synth(settings: SpeechSettings) -> Synth:
    """Load the configured Piper voice; return text -> (wav bytes, seconds)."""
    return make_piper_tone_synths(settings)[2]


def make_piper_tone_synths(settings: SpeechSettings) -> dict[int, Synth]:
    """One synth per call priority, sharing a single loaded voice.

    P1 is the urgent read (quicker, more pitch/energy variation), P3 the calm
    one (a touch slower and flatter), P2 the voice's normal delivery."""
    from piper import PiperVoice, SynthesisConfig

    path = voice_path(settings)
    if not path.exists():
        raise FileNotFoundError(
            f"Piper voice {settings.piper_voice} not found in {settings.voices_dir}/ "
            f"(run: pitwall voice get {settings.piper_voice})"
        )
    voice = PiperVoice.load(path)
    rate = max(-10, min(10, settings.rate))
    base_length = (1.0 - 0.04 * rate) / max(0.5, min(2.0, settings.piper_speed))
    volume = max(0, min(100, settings.volume)) / 100.0
    sample_rate = voice.config.sample_rate
    blip = radio_blip(sample_rate) if settings.radio_click else np.zeros(0)
    tones = {
        1: (settings.tone_urgent_speed, settings.tone_urgent_expression),
        2: (1.0, 1.0),
        3: (settings.tone_calm_speed, settings.tone_calm_expression),
    }

    def make(speed: float, expression: float) -> Synth:
        cfg = SynthesisConfig(
            length_scale=base_length / max(0.5, min(2.0, speed)),
            noise_scale=PIPER_NOISE_SCALE * max(0.0, min(2.0, expression)),
            volume=volume,
        )

        def synth(text: str) -> tuple[bytes, float]:
            parts = [c.audio_float_array for c in voice.synthesize(text, syn_config=cfg)]
            samples = np.concatenate([blip, *parts]) if parts else blip
            return to_wav(samples, sample_rate), len(samples) / sample_rate

        return synth

    return {prio: make(speed, expr) for prio, (speed, expr) in tones.items()}


class PiperRenderer:
    per_priority = True

    def __init__(
        self, tones: dict[int, Synth], synth: Synth, label: str, cache_identity: str
    ) -> None:
        self.tones = tones
        self.synth = synth
        self.label = label
        self.cache_identity = cache_identity

    def render(self, text: str, priority: int, *, background: bool = False) -> tuple[bytes, float]:
        return self.tones.get(priority, self.synth)(text)


def make_piper_speaker(settings: SpeechSettings, player: Player | None = None) -> WavSpeaker:
    tones = make_piper_tone_synths(settings)
    model = voice_path(settings)
    identity = hashlib.sha256(
        json.dumps(
            {
                "settings": settings.model_dump(mode="json"),
                "model_size": model.stat().st_size,
                "model_mtime_ns": model.stat().st_mtime_ns,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    label = f"piper ({settings.piper_voice})"
    renderer = PiperRenderer(tones, tones[2], label, identity)
    return WavSpeaker(
        renderer,
        player or default_player(),
        cache_dir=Path(settings.voices_dir) / ".phrases",
    )


def common_phrases(
    limit: int = 150, extra: Iterable[tuple[str, int]] = ()
) -> list[tuple[str, int]]:
    from pitwall.audio.speaker import REJOIN_OPENER, STARTUP_LINES, STARTUP_PRIORITY

    root = Path(__file__).resolve().parents[1] / "config" / "defaults" / "rules"
    phrases: set[tuple[str, int]] = set()

    def add(text: str, priority: int) -> None:
        if text and "{" not in text:
            phrases.add((text, priority))

    for path in sorted(root.glob("*.yaml")):
        for rule in yaml.safe_load(path.read_text(encoding="utf-8")).get("rules", []):
            definition = RuleDefModel.model_validate(rule)
            priority = definition.priority
            for text in (
                *definition.say_pool(),
                *(text for escalation in definition.escalate for text in escalation.say),
                *definition.say_many,
            ):
                add(text, priority)
            for severity in definition.severity:
                for text in severity.say:
                    add(text, severity.priority or priority)
            for reply in (definition.on_ack, definition.on_neg):
                replies = [reply] if isinstance(reply, str) else reply
                for text in replies:
                    add(text, 1)

    add(REJOIN_OPENER, STARTUP_PRIORITY)
    for text, extra_priority in extra:
        add(text, extra_priority)

    startup = [(text, STARTUP_PRIORITY) for _, text in STARTUP_LINES]
    ordered = startup + sorted(phrases.difference(startup), key=lambda item: (item[1], item[0]))
    return ordered[: max(0, limit)]
