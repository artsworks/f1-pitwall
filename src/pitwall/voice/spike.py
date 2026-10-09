"""Standalone check of the threaded SAPI recogniser on the game PC."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any, cast

from pitwall.audio.dispatcher import Call
from pitwall.audio.speaker import make_speaker
from pitwall.config.models import SpeechSettings, VoiceSettings

from .channel import ChannelRecord, VoiceChannel
from .grammar import VoiceGrammar
from .worker import make_voice_worker


class ProcessStats:
    """CPU seconds and working set of this process."""

    def __init__(self) -> None:
        self._handle: Any = None
        if sys.platform == "win32":
            import win32api  # type: ignore[import-untyped]

            self._handle = win32api.GetCurrentProcess()

    def cpu_s(self) -> float:
        return time.process_time()

    def rss_mb(self) -> tuple[float, float]:
        """Return working set and peak working set in MB."""
        if self._handle is None:
            return 0.0, 0.0
        import win32process  # type: ignore[import-untyped]

        m = win32process.GetProcessMemoryInfo(self._handle)
        return m["WorkingSetSize"] / 2**20, m["PeakWorkingSetSize"] / 2**20

    def lower_priority(self, priority: str, affinity_mask: int) -> str:
        if self._handle is None:
            return "n/a"
        import win32process

        if priority == "below_normal":
            win32process.SetPriorityClass(self._handle, win32process.BELOW_NORMAL_PRIORITY_CLASS)
        if affinity_mask:
            win32process.SetProcessAffinityMask(self._handle, affinity_mask)
        return f"{priority}, affinity {hex(affinity_mask) if affinity_mask else 'default'}"


def record_row(rec: ChannelRecord, cpu_ms: float, rss_mb: float) -> dict[str, Any]:
    row = asdict(rec)
    row["open_ms"] = round(rec.open_ms, 1)
    row["cpu_ms"] = round(cpu_ms, 1)
    row["rss_mb"] = round(rss_mb, 1)
    row["events"] = {k: round(v * 1000.0, 1) for k, v in rec.events.items()}
    sound_end = rec.events.get("sound_end")
    if sound_end is not None and rec.reason in ("recognised", "low_confidence", "miss"):
        row["finalise_ms"] = round(rec.open_ms - sound_end * 1000.0, 1)
    return row


def format_row(row: dict[str, Any]) -> str:
    what = row["intent"] or "-"
    heard = f'"{row["text"]}"' if row["text"] else ""
    extra = f" finalise {row['finalise_ms']:.0f} ms" if "finalise_ms" in row else ""
    via = f" ({row['via']})" if row["via"] else ""
    return (
        f"{row['reason']:<14} {what:<18} {heard} conf {row['confidence']:.2f} "
        f"open {row['open_ms']:.0f} ms{extra} cpu {row['cpu_ms']:.0f} ms "
        f"ws {row['rss_mb']:.0f} MB{via}"
    )


def _stdin_reader(out: queue.Queue[str]) -> None:
    for line in sys.stdin:
        out.put("quit" if line.strip().lower() in ("q", "quit") else "key")
    out.put("quit")


def run_spike(
    voice: VoiceSettings,
    *,
    log_path: Path,
    say: bool = False,
    out: IO[str] = sys.stdout,
) -> int:
    stats = ProcessStats()
    prio = stats.lower_priority(voice.priority, voice.affinity_mask)
    cpu0 = stats.cpu_s()
    rss0, _ = stats.rss_mb()
    worker = make_voice_worker(voice)
    if not worker.wait_ready():
        error = worker.error or RuntimeError("recogniser did not become ready")
        worker.close()
        out.write(f"voice worker unavailable: {error}\n")
        return 1

    rec = cast(Any, worker.recognizer)
    rss1, _ = stats.rss_mb()
    speaker = make_speaker(SpeechSettings(engine="sapi")) if say else None
    inbox: queue.Queue[str] = queue.Queue()
    channel = VoiceChannel(
        worker,
        VoiceGrammar.from_mapping(voice.intents).intent_for,
        max_open_s=voice.max_open_s,
        confidence_min=voice.confidence_min,
    )
    open_cpu = [0.0]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")

    def on_close(record: ChannelRecord) -> None:
        cpu_ms = (stats.cpu_s() - open_cpu[0]) * 1000.0
        row = record_row(record, cpu_ms, stats.rss_mb()[0])
        log.write(json.dumps(row) + "\n")
        log.flush()
        out.write(format_row(row) + "\n")
        out.flush()
        if speaker is not None and record.reason == "recognised" and record.intent:
            t = time.monotonic()
            speaker.speak(
                Call(
                    id=f"voice-spike-{t:.6f}",
                    rule_id="voice-spike",
                    priority=1,
                    text=f"Copy, {record.intent.replace('_', ' ')}.",
                    tags=[],
                    deadline_ms=10000,
                    lap=0,
                    t=t,
                    trigger_t=t,
                )
            )

    channel.on_close = on_close
    grammar = VoiceGrammar.from_mapping(voice.intents)
    fallback = f" (SRGS load failed: {rec.srgs_error})" if rec.grammar_mode == "api" else ""
    out.write(
        f"recogniser: {rec.recognizer_desc} [{rec.lang}]\n"
        f"microphone: {rec.device_desc}\n"
        f"grammar:    {len(grammar.phrases)} phrases / {len(grammar.intents)} intents "
        f"via {rec.grammar_mode}{fallback}\n"
        f"timing:     early close {voice.early_close_ms} ms, silence {voice.close_silence_ms} ms "
        f"(set: {rec.props}), cap {voice.max_open_s:.0f} s\n"
        f"process:    {prio}; load {rec.load_ms:.0f} ms, "
        f"working set {rss0:.0f} -> {rss1:.0f} MB\n"
        f"log:        {log_path}\n"
    )
    out.write("input:      Enter\n")
    out.write("press Enter to open or close, speak, or 'q' + Enter to quit\n")
    out.flush()
    threading.Thread(target=_stdin_reader, args=(inbox,), daemon=True).start()

    t_start = time.monotonic()
    was_open = False
    try:
        while True:
            try:
                kind = inbox.get(timeout=0.01)
            except queue.Empty:
                kind = ""
            now = time.monotonic()
            if kind == "quit":
                break
            if kind == "key":
                channel.key_tap(now)
            for name, text, confidence, event_t in worker.drain():
                if name == "recognised":
                    channel.recognised(text, confidence, event_t)
                elif name == "false":
                    channel.false_recognition(text, confidence, event_t)
                else:
                    channel.mark(name, event_t)
            channel.tick(now)
            if channel.is_open and not was_open:
                open_cpu[0] = stats.cpu_s()
            was_open = channel.is_open
    except KeyboardInterrupt:
        pass
    finally:
        wall = time.monotonic() - t_start
        cpu = stats.cpu_s() - cpu0
        rss, peak = stats.rss_mb()
        out.write(
            f"session: {wall:.0f} s, process cpu {cpu:.1f} s "
            f"({100.0 * cpu / max(wall, 1e-6):.2f} % of one core), "
            f"working set {rss:.0f} MB (peak {peak:.0f} MB)\n"
        )
        log.close()
        worker.close()
        if speaker is not None:
            speaker.close()
    return 0
