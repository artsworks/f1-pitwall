"""Runs candidates with a baseline before and after each. Usage: python drive.py <set>"""
import json, subprocess, sys
from pathlib import Path
HERE = Path(__file__).parent
SETS = {
 "q3": [
  ("tf32off", {"cuda": {"use_tf32": "0"}}),
  ("heur", {"cuda": {"cudnn_conv_algo_search": "HEURISTIC"}}),
  ("exh", {"cuda": {"cudnn_conv_algo_search": "EXHAUSTIVE"}}),
  ("maxws0", {"cuda": {"cudnn_conv_use_max_workspace": "0"}}),
  ("arena", {"cuda": {"arena_extend_strategy": "kSameAsRequested"}}),
  ("copy0", {"cuda": {"do_copy_in_default_stream": "0"}}),
  ("nc1d", {"cuda": {"cudnn_conv1d_pad_to_nc1d": "1"}}),
  ("intra2", {"so": {"intra": 2, "inter": 1}}),
  ("intra1", {"so": {"intra": 1, "inter": 1}}),
  ("optsave", {"so": {"optimized_path": "kokoro-opt.onnx"}}),
  ("optload", {"model": "kokoro-opt.onnx", "so": {"opt": "ORT_DISABLE_ALL"}}),
  ("iobind", {"iobind": True}),
  ("phcache", {"phcache": True}),
 ],
}
keys = ["total_p50", "total_p95", "pass1_p50", "pass23_p50", "run_p50", "ph_p50", "rest_p50", "first_ms", "load_s", "vram_mib", "nan_renders", "peak"]
def run(label, cfg):
    p = subprocess.run([sys.executable, str(HERE / "x.py"), label, json.dumps(cfg)], capture_output=True, text=True)
    try:
        r = json.loads(p.stdout.strip().splitlines()[-1])
        print(f"{label:14s}", " ".join(f"{k}={r[k]}" for k in keys), "sample", r["sample"], flush=True)
    except Exception:
        print(label, "FAILED", p.stdout[-600:], p.stderr[-1500:], flush=True)
name = sys.argv[1]
run(f"{name}-base0", {})
for i, (label, cfg) in enumerate(SETS[name], 1):
    run(label, cfg)
    run(f"{name}-base{i}", {})
