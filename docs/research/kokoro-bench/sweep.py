"""NaN sweep. Usage: python sweep.py <model> <algo> <opt: all|basic|off> [failing-only json]"""
import json, re, sys, time
from pathlib import Path
import numpy as np
import onnxruntime as rt
if hasattr(rt, "preload_dlls"):
    rt.preload_dlls()
from kokoro_onnx import Kokoro

HERE = Path(__file__).parent
MODEL, ALGO, OPT = sys.argv[1:4]
rt.set_default_logger_severity(3)
so = rt.SessionOptions()
so.graph_optimization_level = {"all": rt.GraphOptimizationLevel.ORT_ENABLE_ALL,
                               "basic": rt.GraphOptimizationLevel.ORT_ENABLE_BASIC,
                               "off": rt.GraphOptimizationLevel.ORT_DISABLE_ALL}[OPT]
prov = ["CPUExecutionProvider"] if ALGO == "cpu" else [
    ("CUDAExecutionProvider", {"cudnn_conv_algo_search": ALGO}), "CPUExecutionProvider"]
sess = rt.InferenceSession(str(HERE / MODEL), so, providers=prov)
k = Kokoro.from_session(sess, str(HERE / "voices-v1.0.bin"))
if len(sys.argv) > 4:
    cases = [(c["text"], c["speed"]) for c in json.loads((HERE / sys.argv[4]).read_text(encoding="utf-8"))]
else:
    corpus = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    cases = [(c["text"], s) for c in corpus for s in (0.95, 1.0, 1.1)]
ers = re.compile(r"\bERS\b")
bad, peaks, t0 = [], [], time.perf_counter()
for text, speed in cases:
    voice, _, batches = k._prepare(ers.sub("E R S", text), "bf_emma", speed, "en-gb", False, 0.25, 0.1)
    for ph, _ in batches:
        tokens = k.tokenizer.tokenize(ph)
        audio, _ = k._infer(tokens, k._style_for(voice, len(tokens)), speed)
        nan = int((~np.isfinite(audio)).sum())
        if nan or not len(audio):
            bad.append({"text": text, "speed": speed, "tokens": len(tokens), "len": len(audio), "nonfinite": nan})
        else:
            peaks.append(float(np.abs(audio).max()))
label = f"{MODEL}-{ALGO}-{OPT}"
if len(sys.argv) <= 4:
    (HERE / f"sweep-{label}.json").write_text(json.dumps(bad, indent=1), encoding="utf-8")
print(f"SWEEP {label}: {len(bad)}/{len(cases)} bad, {sum(b['nonfinite'] == b['len'] for b in bad)} fully NaN, "
      f"peak max {max(peaks):.3f}, over 1.0: {sum(p > 1 for p in peaks)}, {time.perf_counter() - t0:.0f}s")
for b in bad[:40]:
    print(" ", b)
