from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

from pitwall.engine.context import EngineContext
from pitwall.input.press import Press
from pitwall.state.session import Snapshot
from pitwall.voice.channel import ChannelRecord, VoiceChannel
from pitwall.voice.grammar import VoiceGrammar
from pitwall.voice.worker import VoiceWorker, make_voice_worker

if TYPE_CHECKING:
    from pitwall.engine.menu import MenuController

log = logging.getLogger(__name__)


class VoiceController:
    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx
        self.menu: MenuController | None = None
        self._voice_worker: VoiceWorker | None = None
        self._voice_closed: list[ChannelRecord] = []

    def reset(self) -> None:
        self._voice_close(self.ctx.clock.now(), "session")

    def setup_voice(self) -> str | None:
        voice = self.ctx.store.current().voice
        if not voice.enabled:
            return None
        if sys.platform != "win32":
            log.warning("voice needs Windows SAPI; disabled")
            print("voice: needs Windows SAPI; disabled", file=sys.stderr)
            return "off (needs Windows)"
        worker: VoiceWorker | None = None
        try:
            worker = make_voice_worker(voice)
            if not worker.wait_ready(10.0):
                raise worker.error or RuntimeError("recogniser did not become ready")
            self.enable_voice(worker)
        except Exception as exc:  # noqa: BLE001 - log voice startup failure and disable voice
            if worker is not None:
                worker.close()
            log.warning("voice unavailable (%s); disabled", exc)
            return f"off ({exc})"
        rec: Any = worker.recognizer
        return f"{rec.recognizer_desc}, mic {rec.device_desc}"

    def enable_voice(self, worker: VoiceWorker) -> None:
        settings = self.ctx.store.current().voice
        grammar = VoiceGrammar.from_mapping(settings.intents)
        self._voice_worker = worker
        self.ctx.voice = VoiceChannel(
            worker,
            grammar.intent_for,
            max_open_s=settings.max_open_s,
            confidence_min=settings.confidence_min,
        )
        self.ctx.voice.on_close = self._voice_closed.append

    def close_voice(self) -> None:
        if self._voice_worker is not None:
            self._voice_worker.close()
            self._voice_worker = None

    def _voice_open(self, t: float, via: str, max_open_s: float | None = None) -> None:
        channel = self.ctx.voice
        if channel is None:
            return
        cap_s = max_open_s or channel.max_open_s
        if channel.open(t, via=via, max_open_s=cap_s) and self.ctx.on_voice_event is not None:
            self.ctx.on_voice_event({"state": "listening", "via": via, "cap_s": cap_s})

    def _voice_close(self, t: float, reason: str) -> None:
        if self.ctx.voice is not None:
            self.ctx.voice.close(t, reason)

    def _voice_tick(self, now: float, snapshot: Snapshot) -> None:
        channel = self.ctx.voice
        worker = self._voice_worker
        if channel is not None and worker is not None:
            for name, text, confidence, _ in worker.drain():
                if name == "recognised":
                    channel.recognised(text, confidence, now)
                elif name == "false":
                    channel.false_recognition(text, confidence, now)
                else:
                    channel.mark(name, now)
            channel.tick(now)
        closed = list(self._voice_closed)
        self._voice_closed.clear()
        for record in closed:
            self._voice_done(record, snapshot)

    def _voice_done(self, record: ChannelRecord, snapshot: Snapshot) -> None:
        items = self.ctx.store.current().menu.items
        item = next((entry for entry in items if entry.id == record.intent), None)
        if self.ctx.on_voice_event is not None:
            self.ctx.on_voice_event(
                {
                    "state": "closed",
                    "reason": record.reason,
                    "text": record.text,
                    "intent": record.intent,
                    "via": record.via,
                    "item": item.label if item is not None else None,
                }
            )
        if record.reason != "recognised" or record.intent is None:
            return
        assert self.menu is not None
        if item is not None:
            if self.ctx.menu.open:
                self.ctx.menu.close()
                self.ctx.dispatcher.cancel_menu_prompt()
                self.menu._menu_log(record.closed_t, snapshot, "menu_close", None, "voice")
            self.menu._menu_answer(item, record.closed_t, snapshot, via="voice")
        elif record.intent in ("ack", "neg"):
            self.ctx.dispatcher.on_press(Press(record.intent, record.closed_t), snapshot)
        elif record.intent in ("mindset_aggressive", "mindset_balanced"):
            self.menu.controls.set_mindset(record.intent.removeprefix("mindset_"), record.closed_t)
        elif record.intent == "page":
            self.menu.controls.reset_page(record.closed_t)

    def voice_payload(self, now: float) -> dict[str, Any]:
        channel = self.ctx.voice
        if channel is None or not channel.is_open or channel.current is None:
            return {
                "available": channel is not None,
                "listening": False,
                "via": "",
                "left_s": None,
                "warn": False,
            }
        elapsed = channel.open_for(now)
        return {
            "available": True,
            "listening": True,
            "via": channel.current.via,
            "left_s": max(0.0, channel.current.cap_s - elapsed),
            "warn": elapsed >= self.ctx.store.current().voice.open_warn_s,
        }
