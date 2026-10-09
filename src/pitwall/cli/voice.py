from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from pitwall.audio.dispatcher import Call
from pitwall.config.loader import ConfigStore


def cmd_voice(args: argparse.Namespace) -> int:
    """Piper voices, speech check and the voice-command channel (docs/21)."""
    action = args.voice_action or "list"
    if action in ("list", "get", "warm"):
        return _voice_piper(action, getattr(args, "names", []))
    if action == "say":
        return _voice_say(args)
    return _voice_channel(action, args)


def _voice_say(args: argparse.Namespace) -> int:
    """Diagnose the audio path: speak TEXT and report when it was spoken."""
    from pitwall.audio.piper_tts import PiperSpeaker, make_piper_synth
    from pitwall.audio.speaker import make_speaker

    store = ConfigStore()
    update: dict[str, object] = {"engine": args.engine}
    if args.voice:
        update["piper_voice"] = args.voice
    if args.speed:
        update["piper_speed"] = args.speed
    speech = store.current().speech.model_copy(update=update)
    if args.save:
        t0 = time.monotonic()
        try:
            wav, seconds = make_piper_synth(speech)(args.text)
        except FileNotFoundError as exc:
            print(exc)
            return 1
        Path(args.save).write_bytes(wav)
        print(
            f"saved {args.save}: {seconds:.1f} s of audio, "
            f"rendered in {(time.monotonic() - t0) * 1000:.0f} ms (incl. voice load)"
        )
        return 0
    speaker = make_speaker(speech)
    print(
        f"speaker: {speaker.name} (engine={args.engine} rate={speech.rate} volume={speech.volume})"
    )
    done = threading.Event()
    spoken_at: list[float] = []

    def _on_spoken(cid: str, t: float) -> None:
        spoken_at.append(time.monotonic())
        done.set()

    speaker.on_spoken = _on_spoken
    t0 = time.monotonic()
    speaker.speak(
        Call(
            id="speak",
            rule_id="speak",
            priority=3,
            text=args.text,
            tags=[],
            deadline_ms=10000,
            lap=0,
            t=t0,
            trigger_t=t0,
        )
    )
    if done.wait(timeout=10.0):
        print(f"spoken after {(spoken_at[0] - t0) * 1000:.0f} ms")
        if isinstance(speaker, PiperSpeaker):
            time.sleep(max(0.0, speaker.busy_until - time.monotonic()))
    else:
        print("TIMEOUT: nothing spoken in 10 s")
    speaker.close()
    return 0


def _voice_piper(action: str, names: list[str]) -> int:
    from pitwall.audio.piper_tts import (
        SUGGESTED_VOICES,
        common_phrases,
        download_voice,
        installed_voices,
        make_piper_speaker,
    )

    speech = ConfigStore().current().speech
    if action == "get":
        for name in names or [speech.piper_voice]:
            path = download_voice(speech, name)
            print(f"downloaded {name} -> {path}")
        return 0
    if action == "warm":
        speaker = make_piper_speaker(speech)
        try:
            count = speaker.warm(common_phrases())
            print(f"cached {count} phrases for {speech.piper_voice}")
        finally:
            speaker.close()
        return 0
    have = installed_voices(speech)
    print(f"voices in {speech.voices_dir}/ (configured: {speech.piper_voice}):")
    for name in have:
        print(f"  {'*' if name == speech.piper_voice else ' '} {name}")
    if not have:
        print("  (none) - run: pitwall voice get")
    print("suggested: " + ", ".join(SUGGESTED_VOICES))
    print("all voices: https://rhasspy.github.io/piper-samples/")
    return 0


def _voice_channel(action: str, args: argparse.Namespace) -> int:
    """Voice-command channel tooling (docs/21): grammar, devices, spike."""
    from pitwall.voice.grammar import VoiceGrammar

    settings = ConfigStore().current()
    voice = settings.voice
    if action == "grammar":
        print(VoiceGrammar.from_mapping(voice.intents).to_srgs(args.lang), end="")
        return 0
    if sys.platform != "win32":
        print("voice devices/spike need Windows SAPI (pywin32)")
        return 1
    if action == "devices":
        from pitwall.voice.sapi import list_inputs

        recs, ins = list_inputs()
        print("recognisers (voice.recognizer matches a substring):")
        for r in recs:
            print(f"  {r}")
        print("audio inputs (voice.device):")
        for i, name in enumerate(ins):
            print(f"  {i}: {name}")
        return 0
    from pitwall.voice.spike import run_spike

    update: dict[str, object] = {}
    if args.device is not None:
        update["device"] = args.device
    if args.recognizer is not None:
        update["recognizer"] = args.recognizer
    if args.confidence is not None:
        update["confidence_min"] = args.confidence
    if args.affinity is not None:
        update["affinity_mask"] = int(args.affinity, 0)
    voice = voice.model_copy(update=update)
    log = Path(args.log or f"recordings/voice-spike-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    return run_spike(
        voice,
        log_path=log,
        say=args.say,
    )
