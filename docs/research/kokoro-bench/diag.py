"""Diagnose fp16 failures. Usage: python diag.py <model file> <cuda|cpu> <passes>"""
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as rt

if hasattr(rt, "preload_dlls"):
    rt.preload_dlls()
from kokoro_onnx import Kokoro

HERE = Path(__file__).parent
MODEL, PROV, PASSES = sys.argv[1], sys.argv[2], int(sys.argv[3])
rt.set_default_logger_severity(3)
providers = ["CPUExecutionProvider"]
if PROV == "cuda":
    providers.insert(0, ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}))
sess = rt.InferenceSession(str(HERE / MODEL), providers=providers)
k = Kokoro.from_session(sess, str(HERE / "voices-v1.0.bin"))
print("providers", sess.get_providers(), "inputs", {i.name: i.type for i in sess.get_inputs()},
      "outputs", {o.name: o.type for o in sess.get_outputs()})

texts = ["Radio check, radio check.", "Radio check."] + [
    l["text"] for l in json.loads((HERE / "lines.json").read_text(encoding="utf-8"))]
rows = []
for n in range(PASSES):
    for i, text in enumerate(texts):
        voice, phonemes, batches = k._prepare(text, "bf_emma", 1.0, "en-gb", False, 0.25, 0.1)
        row = {"pass": n + 1, "i": i, "text": text, "batches": len(batches)}
        tokens = k.tokenizer.tokenize(batches[0][0])
        audio, dur = k._infer(tokens, k._style_for(voice, len(tokens)), 1.0)
        row.update(tokens=len(tokens), raw_len=len(audio), dtype=str(audio.dtype),
                   nan=int(np.isnan(audio).sum()), inf=int(np.isinf(audio).sum()),
                   maxabs=float(np.nanmax(np.abs(audio))) if len(audio) and not np.isnan(audio).all() else None,
                   dur_sum=int(dur.sum()) if dur is not None else None)
        for trim in (True, False):
            try:
                a, _ = k.create(text, voice="bf_emma", speed=1.0, lang="en-gb", trim=trim)
                row[f"trim{trim}"] = f"ok len={len(a)} nan={int(np.isnan(a).sum())}"
            except Exception as e:
                row[f"trim{trim}"] = f"{type(e).__name__}: {e}"[:90]
        rows.append(row)
        bad = row["nan"] or row["inf"] or not row["raw_len"] or not row["trimTrue"].startswith("ok") or " nan=0" not in row["trimTrue"]
        if bad:
            print("BAD", json.dumps(row))
label = f"{MODEL}-{PROV}"
(HERE / f"diag-{label}.json").write_text(json.dumps(rows, indent=1))
ok = sum(r["trimTrue"].startswith("ok") and " nan=0" in r["trimTrue"] and " nan=0" in r["trimFalse"] for r in rows)
print(f"SUMMARY {label}: {ok}/{len(rows)} clean, raw_len range {min(r['raw_len'] for r in rows)}..{max(r['raw_len'] for r in rows)}")
