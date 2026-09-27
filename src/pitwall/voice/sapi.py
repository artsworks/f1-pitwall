"""Windows SAPI 5.4 in-process recogniser (`SAPI.SpInprocRecognizer`) with an
SRGS grammar, driven from one STA thread via pywin32.

All COM calls and events happen on the thread that constructed the object;
call `pump()` from that thread's loop. pywin32 is imported lazily so the module
loads on Linux.

Gating: while the channel is closed the recogniser `State` is inactive, so no
audio is captured and no CPU is spent; `start()` / `stop()` flip it.
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pitwall.voice.grammar import VoiceGrammar, lang_for_lcid

SRS_INACTIVE = 0
SRS_ACTIVE = 1
SLO_STATIC = 0
SGDS_INACTIVE = 0
SGDS_ACTIVE = 1
SRA_TOP_LEVEL = 1
SRA_DYNAMIC = 32

# callback(name, text, confidence, t): name in recognised | false | sound_start |
# sound_end | phrase_start | hypothesis
EventSink = Callable[[str, str, float, float], None]


def _phrase(result: Any) -> tuple[str, float]:
    import win32com.client  # type: ignore[import-untyped]

    res = win32com.client.Dispatch(result)
    info = res.PhraseInfo
    text = str(info.GetText())
    try:
        conf = float(info.Rule.EngineConfidence)
    except Exception:
        conf = 0.0
    return text, conf


class _Events:
    """pywin32 event class for ISpeechRecoContext; `sink` is set after WithEvents."""

    sink: EventSink | None = None

    def _emit(self, name: str, text: str = "", conf: float = 0.0) -> None:
        if self.sink is not None:
            self.sink(name, text, conf, time.monotonic())

    def OnRecognition(self, stream: Any, pos: Any, kind: Any, result: Any) -> None:  # noqa: N802
        self._emit("recognised", *_phrase(result))

    def OnFalseRecognition(self, stream: Any, pos: Any, result: Any) -> None:  # noqa: N802
        try:
            text, conf = _phrase(result)
        except Exception:
            text, conf = "", 0.0
        self._emit("false", text, conf)

    def OnSoundStart(self, stream: Any, pos: Any) -> None:  # noqa: N802
        self._emit("sound_start")

    def OnSoundEnd(self, stream: Any, pos: Any) -> None:  # noqa: N802
        self._emit("sound_end")

    def OnPhraseStart(self, stream: Any, pos: Any) -> None:  # noqa: N802
        self._emit("phrase_start")

    def OnHypothesis(self, stream: Any, pos: Any, result: Any) -> None:  # noqa: N802
        self._emit("hypothesis")


def list_inputs() -> tuple[list[str], list[str]]:
    """(recogniser descriptions, audio input descriptions)."""
    import win32com.client

    reco = win32com.client.Dispatch("SAPI.SpInprocRecognizer")
    recs = reco.GetRecognizers()
    ins = reco.GetAudioInputs()
    return (
        [str(recs.Item(i).GetDescription()) for i in range(recs.Count)],
        [str(ins.Item(i).GetDescription()) for i in range(ins.Count)],
    )


class SapiRecognizer:
    name = "sapi"
    recognizer_desc: str
    device_desc: str
    lang: str
    grammar_mode: str
    load_ms: float
    props: dict[str, bool]
    srgs_error: str = ""
    _reco: Any
    _ctx: Any
    _events: Any
    _grammar: Any

    def __init__(
        self,
        grammar: VoiceGrammar,
        on_event: EventSink,
        *,
        recognizer: str = "",
        device: int = 0,
        early_close_ms: int = 300,
        close_silence_ms: int = 1000,
        grammar_mode: str = "srgs",
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("SapiRecognizer requires Windows")
        import pythoncom  # type: ignore[import-untyped]
        import win32com.client

        pythoncom.CoInitialize()
        self._pythoncom: Any = pythoncom
        t0 = time.monotonic()
        reco: Any = win32com.client.gencache.EnsureDispatch("SAPI.SpInprocRecognizer")
        recs = reco.GetRecognizers()
        if recs.Count == 0:
            raise RuntimeError("no SAPI recogniser installed (Settings > Time & language > Speech)")
        token = recs.Item(0)
        if recognizer:
            for i in range(recs.Count):
                if recognizer.lower() in str(recs.Item(i).GetDescription()).lower():
                    token = recs.Item(i)
                    break
            else:
                raise RuntimeError(f"no SAPI recogniser matches {recognizer!r}")
        reco.Recognizer = token
        self.recognizer_desc = str(token.GetDescription())
        try:
            self.lang = lang_for_lcid(str(token.GetAttribute("Language")))
        except Exception:
            self.lang = "en-US"
        ins = reco.GetAudioInputs()
        if ins.Count == 0:
            raise RuntimeError("no audio input device")
        if not 0 <= device < ins.Count:
            raise RuntimeError(f"audio input {device} out of range (0..{ins.Count - 1})")
        reco.AudioInput = ins.Item(device)
        self.device_desc = str(ins.Item(device).GetDescription())
        self.props: dict[str, bool] = {}
        for prop, value in (
            ("CompleteResponseSpeed", early_close_ms),
            ("IncompleteResponseSpeed", close_silence_ms),
        ):
            try:
                self.props[prop] = bool(reco.SetPropertyNumber(prop, value))
            except Exception:
                self.props[prop] = False
        ctx: Any = reco.CreateRecoContext()
        events: Any = win32com.client.WithEvents(ctx, _Events)
        events.sink = on_event
        g: Any = ctx.CreateGrammar(1)
        self.grammar_mode = self._load(g, grammar, grammar_mode)
        g.CmdSetRuleIdState(0, SGDS_ACTIVE)
        reco.State = SRS_INACTIVE
        self.load_ms = (time.monotonic() - t0) * 1000.0
        self._reco = reco
        self._ctx = ctx
        self._events = events
        self._grammar = g

    def _load(self, g: Any, grammar: VoiceGrammar, mode: str) -> str:
        if mode == "srgs":
            path = Path(tempfile.gettempdir()) / "pitwall-voice.grxml"
            path.write_text(grammar.to_srgs(self.lang), encoding="utf-8")
            try:
                g.CmdLoadFromFile(str(path), SLO_STATIC)
                return "srgs"
            except Exception as exc:  # fall back to building the same rule in code
                self.srgs_error = str(exc)
        rule = g.Rules.Add("request", SRA_TOP_LEVEL | SRA_DYNAMIC, 1)
        for phrase in grammar.phrases:
            rule.InitialState.AddWordTransition(None, phrase)
        g.Rules.Commit()
        return "api"

    def start(self) -> None:
        self._reco.State = SRS_ACTIVE

    def stop(self) -> None:
        self._reco.State = SRS_INACTIVE

    def pump(self) -> None:
        self._pythoncom.PumpWaitingMessages()

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self._events = None
            self._grammar = None
            self._ctx = None
            self._reco = None
