"""Render benchmark. Usage: python bench.py <mode> <label>

modes: kokoro        Kokoro(...) as kokoro-onnx builds the session
       kokoro-cuda   CUDA session with cudnn_conv_algo_search=DEFAULT
       piper         the Pitwall Piper synth (run from the repo venv)
"""
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
MODE, LABEL = sys.argv[1], sys.argv[2]
PASSES = 3
lines = json.loads((HERE / "lines.json").read_text(encoding="utf-8"))


def vram_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    ).stdout
    return int(out.strip().splitlines()[0])


vram_before = vram_mib()
t0 = time.perf_counter()
if MODE == "piper":
    import io

    from pitwall.audio.piper_tts import make_piper_tone_synths
    from pitwall.config.loader import ConfigStore

    speech = ConfigStore().current().speech
    synth = make_piper_tone_synths(speech)[2]
    providers = [f"piper {speech.piper_voice} speed {speech.piper_speed}"]

    def render(text: str) -> tuple[np.ndarray, int]:
        wav, _ = synth(text)
        with wave.open(io.BytesIO(wav), "rb") as r:
            pcm = np.frombuffer(r.readframes(r.getnframes()), dtype="<i2")
            return pcm.astype(np.float32) / 32767.0, r.getframerate()
else:
    import onnxruntime as rt

    if hasattr(rt, "preload_dlls"):
        rt.preload_dlls()
    from kokoro_onnx import Kokoro

    model, voices = str(HERE / __import__("os").environ.get("KOKORO_MODEL", "kokoro-v1.0.onnx")), str(HERE / "voices-v1.0.bin")
    if MODE == "kokoro-cuda":
        session = rt.InferenceSession(
            model,
            providers=[
                ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}),
                "CPUExecutionProvider",
            ],
        )
        kokoro = Kokoro.from_session(session, voices)
    else:
        kokoro = Kokoro(model, voices)
    providers = kokoro.sess.get_providers()

    def render(text: str) -> tuple[np.ndarray, int]:
        return kokoro.create(text, voice=os.environ.get("KOKORO_VOICE", "bf_emma"), speed=1.0, lang=os.environ.get("KOKORO_LANG", "en-gb"))
load_s = time.perf_counter() - t0

t0 = time.perf_counter()
samples, rate = render("Radio check, radio check.")
first_s = time.perf_counter() - t0
with wave.open(str(HERE / f"sample-{LABEL}.wav"), "wb") as w:
    s, r = render("Box this lap for mediums. Norris pitted on lap twenty four")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(r)
    w.writeframes((np.clip(s, -1, 1) * 32767).astype("<i2").tobytes())

rows = []
for n in range(PASSES):
    for line in lines:
        t0 = time.perf_counter()
        samples, rate = render(line["text"])
        dt = time.perf_counter() - t0
        rows.append({"pass": n + 1, "chars": len(line["text"]), "kind": line["kind"],
                     "ms": dt * 1000, "audio_s": len(samples) / rate})
vram_after = vram_mib()


def stats(sel: list[dict]) -> dict:
    ms = np.array([r["ms"] for r in sel])
    audio = sum(r["audio_s"] for r in sel)
    return {"n": len(sel), "p50_ms": round(float(np.percentile(ms, 50))),
            "p95_ms": round(float(np.percentile(ms, 95))), "max_ms": round(float(ms.max())),
            "rtf": round(float(ms.sum() / 1000 / audio), 3)}


result = {
    "label": LABEL, "mode": MODE, "providers": providers, "sample_rate": rate,
    "load_s": round(load_s, 2), "first_render_ms": round(first_s * 1000),
    "vram_delta_mib": vram_after - vram_before,
    "pass1": stats([r for r in rows if r["pass"] == 1]),
    "pass2_3": stats([r for r in rows if r["pass"] > 1]),
    "all": stats(rows),
    "short_le30": stats([r for r in rows if r["chars"] <= 30]),
    "long_gt45": stats([r for r in rows if r["chars"] > 45]),
    "audio_s_mean": round(float(np.mean([r["audio_s"] for r in rows])), 2),
}
(HERE / f"result-{LABEL}.json").write_text(json.dumps(result, indent=1))
print(json.dumps(result, indent=1))

