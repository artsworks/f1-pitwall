#!/usr/bin/env python3
"""Measure TTS synthesis latency for one lap of max-budget radio chatter.

Runs unchanged on any branch: Piper via `make_piper_tone_synths`, Kokoro via
`make_kokoro_tone_synths` where `pitwall.audio.kokoro_tts` exists. Reads the
configured `speech` settings and changes nothing.

Usage: uv run python scripts/perf_tts.py [--engine auto|piper|kokoro]
       [--voice NAME] [--speed X] [--repeat N]
"""

from __future__ import annotations

import argparse
import importlib
import io
import os
import statistics
import sys
import time
import wave
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from pitwall.audio import piper_tts  # noqa: E402
from pitwall.config.loader import ConfigStore  # noqa: E402
from pitwall.config.models import SpeechSettings  # noqa: E402

Synth = Callable[[str], tuple[bytes, float]]

# One lap at the top `budget_steps` step (menu.yaml): 20 budgeted P2/P3 calls,
# plus P1 calls, which the budget never caps. "rule" lines are fixed phrases
# from the rule YAML; "filled" lines are rule templates with example values.
LAP: tuple[tuple[int, str, str], ...] = (
    (1, "rule", "Safety car, safety car. Watch the delta"),
    (1, "rule", "Lost the front wing. Box this lap"),
    (2, "rule", "Tyres are running hot. Ease the slides"),
    (2, "rule", "Save the rear left. Short-shift out of right-handers"),
    (2, "rule", "Recharge mode. Harvest, easy on tyres"),
    (2, "rule", "Track limits warning. Stay inside the kerbs"),
    (2, "rule", "Fighting your team-mate. Race hard, race clean"),
    (2, "rule", "No DRS. Work the exits"),
    (2, "rule", "Graining. Load the fronts, it'll clean up"),
    (2, "rule", "Box this lap, it's not going to be enough"),
    (3, "rule", "Gaps stable. Manage tyres"),
    (3, "rule", "Clean air. All yours."),
    (3, "rule", "All under control. Save tyres"),
    (3, "rule", "No new damage from that contact"),
    (3, "rule", "Gaps holding. Nice and tidy, save the tyres"),
    (3, "filled", "About 12 laps left in the tyres"),
    (3, "filled", "Fuel is good, 0.8 laps spare. Use it"),
    (3, "filled", "Still on Plan A, window laps 18 to 22"),
    (3, "filled", "1.4 MJ unused last lap. Deploy more"),
    (3, "filled", "Yellow behind, sector 2. You're clear."),
    (3, "filled", "Gap covers the penalty. P6 safe"),
    (3, "filled", "And they're back. Good recovery, P7"),
)
WARMUP_TEXT = "Radio check."


class _NullPlayer:
    def play(self, wav: bytes) -> None:
        pass

    def stop(self) -> None:
        pass


def _summary(values: list[float]) -> str:
    ordered = sorted(values)
    if not ordered:
        return "n/a"
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[min(int(len(ordered) * 0.99), len(ordered) - 1)]
    return f"mean {statistics.mean(ordered):.1f} p50 {p50:.1f} p99 {p99:.1f}"


def _kokoro_module() -> ModuleType | None:
    try:
        return importlib.import_module("pitwall.audio.kokoro_tts")
    except ModuleNotFoundError as exc:
        if exc.name == "pitwall.audio.kokoro_tts":
            return None
        raise


def _settings(args: argparse.Namespace, kokoro: ModuleType | None) -> tuple[str, SpeechSettings]:
    speech = ConfigStore().current().speech
    engine = args.engine or speech.engine
    if engine == "auto":
        engine = "kokoro" if kokoro is not None and kokoro.kokoro_installed(speech) else "piper"
    prefix = "kokoro" if engine == "kokoro" else "piper"
    update: dict[str, object] = {}
    if args.voice:
        update[f"{prefix}_voice"] = args.voice
    if args.speed:
        update[f"{prefix}_speed"] = args.speed
    return engine, speech.model_copy(update=update)


def _load(engine: str, speech: SpeechSettings, kokoro: ModuleType | None) -> dict[int, Synth]:
    if engine == "piper":
        synths: dict[int, Synth] = piper_tts.make_piper_tone_synths(speech)
        return synths
    if engine == "kokoro":
        if kokoro is None:
            raise RuntimeError("this checkout has no Kokoro backend (pitwall.audio.kokoro_tts)")
        synths = kokoro.make_kokoro_tone_synths(speech)
        return synths
    raise RuntimeError(f"engine {engine!r} has no synth to benchmark (use piper or kokoro)")


def _sample_rate(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav)) as reader:
        return int(reader.getframerate())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["auto", "piper", "kokoro"], default=None)
    parser.add_argument("--voice", help="voice name for the chosen engine")
    parser.add_argument("--speed", type=float, help="pace multiplier for the chosen engine")
    parser.add_argument("--repeat", type=int, default=3, help="cold passes over the lap")
    args = parser.parse_args()

    kokoro = _kokoro_module()
    engine, speech = _settings(args, kokoro)
    start = time.perf_counter()
    try:
        tones = _load(engine, speech, kokoro)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"perf_tts: {engine} backend unavailable: {exc}", file=sys.stderr)
        return 2
    load_ms = (time.perf_counter() - start) * 1000
    voice = getattr(speech, f"{engine}_voice")
    start = time.perf_counter()
    wav, _ = tones[2](WARMUP_TEXT)
    first_ms = (time.perf_counter() - start) * 1000
    sample_rate = _sample_rate(wav)

    cold: list[list[float]] = [[] for _ in LAP]
    audio_s = [0.0] * len(LAP)
    for _ in range(max(1, args.repeat)):
        for i, (priority, _, text) in enumerate(LAP):
            start = time.perf_counter()
            _, seconds = tones[priority](text)
            cold[i].append((time.perf_counter() - start) * 1000)
            audio_s[i] = seconds
    cold_ms = [statistics.median(samples) for samples in cold]

    speaker = piper_tts.PiperSpeaker(tones[2], _NullPlayer(), label=engine, tones=tones)
    render_cold: list[float] = []
    render_warm: list[float] = []
    for target in (render_cold, render_warm):
        for priority, _, text in LAP:
            start = time.perf_counter()
            speaker.render(text, priority)
            target.append((time.perf_counter() - start) * 1000)
    speaker.close()

    phrases = getattr(piper_tts, "common_phrases", None)
    warm_set = set(phrases()) if phrases is not None else None
    print(
        f"{'#':>2} {'prio':>4} {'src':6} {'warm':4} {'audio_s':>7} {'cold_ms':>8} {'rtf':>5}  text"
    )
    for i, (priority, source, text) in enumerate(LAP):
        cached = "n/a" if warm_set is None else "yes" if (text, priority) in warm_set else "no"
        rtf = cold_ms[i] / 1000 / audio_s[i] if audio_s[i] else 0.0
        print(
            f"{i + 1:2d} {'P' + str(priority):>4} {source:6} {cached:4} "
            f"{audio_s[i]:7.2f} {cold_ms[i]:8.1f} {rtf:5.2f}  {text}"
        )

    budgeted = [i for i, (priority, _, _) in enumerate(LAP) if priority != 1]
    print("== perf_tts summary ==")
    print(f"engine          {engine}")
    print(f"voice           {voice}")
    print(f"sample_rate     {sample_rate}")
    print(f"cpu_count       {os.cpu_count()}")
    print(f"repeat          {max(1, args.repeat)} (per-call value = median)")
    print(f"load_ms         {load_ms:.0f}")
    print(f"first_synth_ms  {first_ms:.1f}")
    p1_count = len(LAP) - len(budgeted)
    print(f"calls           {len(LAP)} ({len(budgeted)} budgeted P2/P3 + {p1_count} P1)")
    print(f"lap_audio_s     {sum(audio_s):.1f}")
    print(f"lap_cold_ms     {sum(cold_ms):.0f}")
    print(f"budget_cold_ms  {sum(cold_ms[i] for i in budgeted):.0f}")
    for priority in (1, 2, 3):
        values = [cold_ms[i] for i, (p, _, _) in enumerate(LAP) if p == priority]
        print(f"P{priority} cold_ms      {_summary(values)} (n={len(values)})")
    print(f"all cold_ms     {_summary(cold_ms)}")
    print(f"render cold_ms  {_summary(render_cold)} total {sum(render_cold):.0f}")
    print(f"render warm_ms  {_summary(render_warm)} total {sum(render_warm):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
