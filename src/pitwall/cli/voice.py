from __future__ import annotations

import argparse
import asyncio
import re
import string
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from pitwall.audio.dispatcher import Call
from pitwall.config.loader import ConfigStore
from pitwall.config.paths import storage_path

_MEASURED_WORDS = frozenset({"battle_pace_ahead", "battle_pace_behind"})
_INTEGER = re.compile(r"-?\d+")


def _volatile_slot(name: str, spec: str, value: str) -> bool:
    return (
        name.endswith(("_name", "_pct"))
        or name in _MEASURED_WORDS
        or spec.endswith(("f", "e", "g", "%"))
        or (any(c.isdigit() for c in value) and not _INTEGER.fullmatch(value))
    )


def _reusable(text: str, templates: Sequence[str]) -> bool:
    """Return whether a rendered line can be reused across sessions."""
    if "{" in text:
        return False

    matched = False
    formatter = string.Formatter()
    for template in templates:
        pattern: list[str] = []
        fields: list[tuple[str, str]] = []
        for literal, field_name, format_spec, _conversion in formatter.parse(template):
            pattern.append(re.escape(literal))
            if field_name is not None:
                pattern.append("(.+?)")
                fields.append((field_name, format_spec or ""))

        if not fields:
            if text == template:
                return True
            continue

        match = re.fullmatch("".join(pattern), text, flags=re.S)
        if match is None:
            continue
        matched = True
        if all(
            not _volatile_slot(name, spec, value)
            for (name, spec), value in zip(fields, match.groups(), strict=True)
        ):
            return True
    return not matched


def cmd_voice(args: argparse.Namespace) -> int:
    """Piper voices, speech check and the voice-command channel (docs/21)."""
    action = args.voice_action or "list"
    if action == "warm":
        return _voice_warm(args)
    if action in ("list", "get"):
        return _voice_piper(action, getattr(args, "names", []))
    if action == "say":
        return _voice_say(args)
    return _voice_channel(action, args)


def _voice_say(args: argparse.Namespace) -> int:
    """Diagnose the audio path: speak TEXT and report when it was spoken."""
    from pitwall.audio.piper_tts import make_piper_synth
    from pitwall.audio.speaker import make_speaker

    store = ConfigStore()
    update: dict[str, object] = {}
    if args.engine is not None:
        update["engine"] = args.engine
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
        f"speaker: {speaker.name} (engine={speech.engine} "
        f"rate={speech.rate} volume={speech.volume})"
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
        busy_until = getattr(speaker, "busy_until", time.monotonic())
        time.sleep(max(0.0, busy_until - time.monotonic()))
    else:
        print("TIMEOUT: nothing spoken in 10 s")
    speaker.close()
    return 0


def _voice_piper(action: str, names: list[str]) -> int:
    from pitwall.audio.piper_tts import (
        SUGGESTED_VOICES,
        download_voice,
        installed_voices,
    )

    speech = ConfigStore().current().speech
    if action == "get":
        for name in names or [speech.piper_voice]:
            path = download_voice(speech, name)
            print(f"downloaded {name} -> {path}")
        return 0
    have = installed_voices(speech)
    print(f"voices in {storage_path(speech.voices_dir)}/ (configured: {speech.piper_voice}):")
    for name in have:
        print(f"  {'*' if name == speech.piper_voice else ' '} {name}")
    if not have:
        print("  (none) - run: pitwall voice get")
    print("suggested: " + ", ".join(SUGGESTED_VOICES))
    print("all voices: https://rhasspy.github.io/piper-samples/")
    return 0


def _voice_warm(args: argparse.Namespace) -> int:
    from pitwall.audio.piper_tts import common_phrases
    from pitwall.audio.speaker import (
        ENGINES,
        STARTUP_LINES,
        STARTUP_PRIORITY,
        WarmableSpeaker,
        auto_engine,
    )

    settings = ConfigStore().current()
    speech = settings.speech
    engine = args.engine if args.engine != "auto" else speech.engine
    if engine == "auto":
        engine = auto_engine(speech)
    if engine not in ENGINES:
        print(f"unknown speech engine: {engine}")
        return 1
    try:
        speaker = ENGINES[engine].factory(speech)
    except Exception as exc:  # noqa: BLE001 - print the unavailable engine to the CLI
        print(f"{engine} unavailable: {exc}")
        return 1
    if not isinstance(speaker, WarmableSpeaker):
        print(f"{speaker.name} does not support phrase warming")
        speaker.close()
        return 1

    try:
        if args.replay:
            from pitwall.cli.common import RecordingNotFoundError, _resolve_recording
            from pitwall.clock import VirtualClock
            from pitwall.engine import build_engine, run_replay

            try:
                path = _resolve_recording(args.replay, settings)
            except RecordingNotFoundError as exc:
                print(f"voice warm: {exc}")
                return 1
            with tempfile.TemporaryDirectory(prefix="pitwall-voice-warm-") as directory:
                replay_engine = build_engine(
                    clock=VirtualClock(),
                    sinks=[],
                    decision_log_path=Path(directory) / "decisions.jsonl",
                )
                try:
                    print(f"voice warm: replaying {path.name} ...", file=sys.stderr)
                    _, calls = asyncio.run(run_replay(path, replay_engine, None))
                finally:
                    replay_engine.dispatcher.log.close()
            rule_engine = replay_engine.rule_engine
            templates: dict[str, list[str]] = {}
            if rule_engine is not None:
                for rule in rule_engine.rules:
                    definition = rule.defn
                    templates[definition.id] = [
                        *definition.say_pool(),
                        *(text for escalation in definition.escalate for text in escalation.say),
                        *(text for severity in definition.severity for text in severity.say),
                        *definition.say_many,
                    ]
            dropped_texts = {
                call.text
                for call in calls
                if not _reusable(call.text, templates.get(call.rule_id, ()))
            }
            replay_phrases = sorted(
                {
                    (call.text, call.priority)
                    for call in calls
                    if _reusable(call.text, templates.get(call.rule_id, ()))
                },
                key=lambda phrase: (phrase[1], phrase[0]),
            )
            startup = [(text, STARTUP_PRIORITY) for _, text in STARTUP_LINES]
            phrases = list(dict.fromkeys([*startup, *replay_phrases]))
        else:
            extra = [
                (reply, 1)
                for reply in (
                    *settings.input.ack_replies,
                    *settings.input.neg_replies,
                    *settings.input.bookmark_replies,
                )
            ]
            if settings.menu.speak_on_scroll:
                extra.extend((item.label, 1) for item in settings.menu.items)
            phrases = common_phrases(speech.warm_phrases, extra)
        if args.replay and dropped_texts:
            print(f"voice warm: skipped {len(dropped_texts)} lines with names or measured values")
        count = speaker.warm(phrases)
        print(f"cached {count} phrases for {speaker.name}")
        if engine == "omnivoice":
            from pitwall.audio.omnivoice_tts import render_stats_summary

            for mode in ("sync", "background"):
                summary = render_stats_summary(mode)
                if summary is not None:
                    print(summary)
        return 0
    finally:
        speaker.close()


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
    log = (
        Path(args.log)
        if args.log
        else storage_path(settings.recording.directory)
        / f"voice-spike-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    return run_spike(
        voice,
        log_path=log,
        say=args.say,
    )
