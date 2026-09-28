"""`pitwall voice spike`: Phase 0 check of the SAPI recogniser on the game PC.

Tap UDP Action 1 (wheel) or press Enter to open the channel, say a phrase, and
the channel closes on its own. Every channel is printed and appended to a JSONL
log with latency, confidence, process CPU and working set, so accuracy and cost
can be judged from a real session. Runs standalone (binds the telemetry port);
stop `pitwall start` first, or point the game at `--port`.
"""

from __future__ import annotations

import json
import queue
import socket
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

from pitwall.config.models import InputSettings, VoiceSettings
from pitwall.input.press import PressDetector
from pitwall.protocol.header import HEADER_SIZE, PacketId

from .channel import ChannelRecord, VoiceChannel
from .grammar import VoiceGrammar

BUTN = b"BUTN"
EVENT_CODE_AT = HEADER_SIZE
PACKET_ID_AT = 6


def butn_status(payload: bytes) -> int | None:
    """Button bitmask of a BUTN event datagram, else None."""
    end = EVENT_CODE_AT + 8
    if len(payload) < end or payload[PACKET_ID_AT] != PacketId.EVENT:
        return None
    if payload[EVENT_CODE_AT : EVENT_CODE_AT + 4] != BUTN:
        return None
    return int.from_bytes(payload[EVENT_CODE_AT + 4 : end], "little")


class ProcessStats:
    """CPU seconds and working set of this process (the in-proc recogniser runs here)."""

    def __init__(self) -> None:
        self._handle: Any = None
        if sys.platform == "win32":
            import win32api  # type: ignore[import-untyped]

            self._handle = win32api.GetCurrentProcess()

    def cpu_s(self) -> float:
        return time.process_time()

    def rss_mb(self) -> tuple[float, float]:
        """(working set, peak working set) in MB; (0, 0) off Windows."""
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
    via = f" ({row['passthrough']})" if row["passthrough"] else ""
    return (
        f"{row['reason']:<14} {what:<18} {heard} conf {row['confidence']:.2f} "
        f"open {row['open_ms']:.0f} ms{extra} cpu {row['cpu_ms']:.0f} ms "
        f"ws {row['rss_mb']:.0f} MB{via}"
    )


def _udp_reader(host: str, port: int, bit: int, out: queue.Queue[tuple[str, float, bool]]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    down = False
    while True:
        payload = sock.recv(2048)
        status = butn_status(payload)
        if status is None:
            continue
        now = bool(status & bit)
        if now != down:
            down = now
            out.put(("edge", time.monotonic(), now))


def _stdin_reader(out: queue.Queue[tuple[str, float, bool]]) -> None:
    for line in sys.stdin:
        out.put(
            ("quit" if line.strip().lower() in ("q", "quit") else "key", time.monotonic(), True)
        )
    out.put(("quit", time.monotonic(), True))


def run_spike(
    voice: VoiceSettings,
    inp: InputSettings,
    *,
    host: str,
    port: int | None,
    log_path: Path,
    grammar_mode: str = "srgs",
    say: bool = False,
    out: IO[str] = sys.stdout,
) -> int:
    from .sapi import SapiRecognizer

    grammar = VoiceGrammar.from_mapping(voice.intents)
    stats = ProcessStats()
    prio = stats.lower_priority(voice.priority, voice.affinity_mask)
    inbox: queue.Queue[tuple[str, float, bool]] = queue.Queue()
    channel_ref: list[VoiceChannel] = []

    def on_event(name: str, text: str, conf: float, t: float) -> None:
        ch = channel_ref[0]
        if name == "recognised":
            ch.recognised(text, conf, t)
        elif name == "false":
            ch.false_recognition(text, conf, t)
        else:
            ch.mark(name, t)

    cpu0 = stats.cpu_s()
    rss0, _ = stats.rss_mb()
    rec = SapiRecognizer(
        grammar,
        on_event,
        recognizer=voice.recognizer,
        device=voice.device,
        early_close_ms=voice.early_close_ms,
        close_silence_ms=voice.close_silence_ms,
        grammar_mode=grammar_mode,
    )
    rss1, _ = stats.rss_mb()
    speaker: Any = None
    if say:
        import win32com.client  # type: ignore[import-untyped]

        speaker = win32com.client.Dispatch("SAPI.SpVoice")
    channel = VoiceChannel(
        rec,
        grammar.intent_for,
        max_open_s=voice.max_open_s,
        confidence_min=voice.confidence_min,
        detector=PressDetector(inp.double_press_ms, inp.long_press_ms, inp.bounce_ms),
    )
    channel_ref.append(channel)
    open_cpu: list[float] = [0.0]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")

    def on_close(r: ChannelRecord) -> None:
        cpu_ms = (stats.cpu_s() - open_cpu[0]) * 1000.0
        row = record_row(r, cpu_ms, stats.rss_mb()[0])
        log.write(json.dumps(row) + "\n")
        log.flush()
        out.write(format_row(row) + "\n")
        out.flush()
        if speaker is not None and r.reason == "recognised" and r.intent:
            speaker.Speak(f"Copy, {r.intent.replace('_', ' ')}.", 1)

    channel.on_close = on_close
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
    if port is not None:
        threading.Thread(
            target=_udp_reader, args=(host, port, inp.udp_action_bit, inbox), daemon=True
        ).start()
        out.write(f"input:      UDP {host}:{port} Action bit {hex(inp.udp_action_bit)}, or Enter\n")
    else:
        out.write("input:      Enter\n")
    out.write("tap to open, speak, the channel closes by itself; 'q' + Enter quits\n")
    out.flush()
    threading.Thread(target=_stdin_reader, args=(inbox,), daemon=True).start()

    t_start = time.monotonic()
    was_open = False
    try:
        while True:
            rec.pump()
            try:
                kind, t, down = inbox.get(timeout=0.01)
            except queue.Empty:
                kind = ""
                t, down = time.monotonic(), False
            if kind == "quit":
                break
            if kind == "edge":
                channel.button_edge(t, down)
            elif kind == "key":
                channel.key_tap(t)
            channel.tick(time.monotonic())
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
        rec.close()
        log.close()
    return 0
