"""One-config experiment. Usage: python x.py <label> '<json config>'

config keys: cuda {provider options}, so {intra, inter, mode, opt, optimized_path},
trt {provider options} (TensorRT first), cpu true, iobind true, phcache true, profile true, passes N
"""
import json, os, subprocess, sys, time, wave
from pathlib import Path
import numpy as np
import onnxruntime as rt
if hasattr(rt, "preload_dlls"):
    rt.preload_dlls()
from kokoro_onnx import Kokoro

HERE = Path(__file__).parent
LABEL = sys.argv[1]
CFG = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
VOICE, LANG = "af_heart", "en-us"
SAMPLE = "Box this lap for mediums. Norris pitted on lap twenty four"
lines = json.loads((HERE / "lines.json").read_text(encoding="utf-8"))


def vram() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])


rt.set_default_logger_severity(CFG.get("log", 3))
so = rt.SessionOptions()
s = CFG.get("so", {})
if "intra" in s: so.intra_op_num_threads = s["intra"]
if "inter" in s: so.inter_op_num_threads = s["inter"]
if "mode" in s: so.execution_mode = getattr(rt.ExecutionMode, s["mode"])
if "opt" in s: so.graph_optimization_level = getattr(rt.GraphOptimizationLevel, s["opt"])
if "optimized_path" in s: so.optimized_model_filepath = str(HERE / s["optimized_path"])
if CFG.get("profile"):
    so.enable_profiling = True
    so.profile_file_prefix = str(HERE / f"prof-{LABEL}")
cuda = {"cudnn_conv_algo_search": "DEFAULT", **CFG.get("cuda", {})}
providers = [("CUDAExecutionProvider", cuda), "CPUExecutionProvider"]
if "trt" in CFG:
    import tensorrt_libs
    os.environ["PATH"] = os.path.dirname(tensorrt_libs.__file__) + os.pathsep + os.environ["PATH"]
    providers.insert(0, ("TensorrtExecutionProvider", CFG["trt"]))
if CFG.get("cpu"):
    providers = ["CPUExecutionProvider"]
model = str(HERE / CFG.get("model", "kokoro-v1.0.onnx"))

v0 = vram()
t0 = time.perf_counter()
sess = rt.InferenceSession(model, so, providers=providers)
k = Kokoro.from_session(sess, str(HERE / "voices-v1.0.bin"))
load_s = time.perf_counter() - t0

T = {"ph": 0.0, "run": 0.0}
orig_ph = k.tokenizer.phonemize
cache: dict = {}


def ph(text, lang="en-us", norm=True):
    t = time.perf_counter()
    if CFG.get("phcache") and (text, lang) in cache:
        out = cache[(text, lang)]
    else:
        out = orig_ph(text, lang, norm)
        cache[(text, lang)] = out
    T["ph"] += time.perf_counter() - t
    return out


k.tokenizer.phonemize = ph


class Sess:
    def __getattr__(self, name):
        return getattr(sess, name)

    def run(self, names, inputs):
        t = time.perf_counter()
        if CFG.get("iobind"):
            b = sess.io_binding()
            for name, arr in inputs.items():
                b.bind_cpu_input(name, arr)
            for o in sess.get_outputs():
                b.bind_output(o.name, "cuda")
            sess.run_with_iobinding(b)
            out = b.copy_outputs_to_cpu()
        else:
            out = sess.run(names, inputs)
        T["run"] += time.perf_counter() - t
        return out


k.sess = Sess()


def render(text, speed=1.0):
    T["ph"] = T["run"] = 0.0
    t = time.perf_counter()
    a, r = k.create(text, voice=VOICE, speed=speed, lang=LANG)
    total = time.perf_counter() - t
    return a, r, total * 1000, T["ph"] * 1000, T["run"] * 1000


a, r, first_ms, _, _ = render("Radio check, radio check.")
a, r, *_ = render(SAMPLE)
with wave.open(str(HERE / f"sample-x-{LABEL}.wav"), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(r)
    w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
sample = {"nan": int((~np.isfinite(a)).sum()), "peak": round(float(np.abs(a).max()), 3), "len": len(a)}

rows, nan_renders, peak = [], 0, 0.0
for n in range(CFG.get("passes", 3)):
    for line in lines:
        a, r, tot, p, run = render(line["text"])
        bad = int((~np.isfinite(a)).sum())
        nan_renders += bool(bad)
        if not bad:
            peak = max(peak, float(np.abs(a).max()))
        rows.append((tot, p, run, tot - p - run, len(line["text"]), n))
v1 = vram()
if CFG.get("profile"):
    print("profile:", sess.end_profiling())
m = np.array(rows)
pc = lambda c, q: round(float(np.percentile(m[:, c], q)), 1)
res = {"label": LABEL, "cfg": CFG, "providers": sess.get_providers(), "load_s": round(load_s, 2),
       "first_ms": round(first_ms), "vram_mib": v1 - v0, "n": len(rows),
       "total_p50": pc(0, 50), "total_p95": pc(0, 95), "total_max": round(float(m[:, 0].max()), 1),
       "ph_p50": pc(1, 50), "ph_p95": pc(1, 95), "run_p50": pc(2, 50), "run_p95": pc(2, 95),
       "pass1_p50": round(float(np.percentile(m[m[:, 5] == 0, 0], 50)), 1), "pass23_p50": round(float(np.percentile(m[m[:, 5] > 0, 0], 50)), 1), "rest_p50": pc(3, 50), "rest_p95": pc(3, 95),
       "nan_renders": nan_renders, "peak": round(peak, 3), "sample": sample}
(HERE / f"result-x-{LABEL}.json").write_text(json.dumps(res, indent=1))
print(json.dumps(res))


