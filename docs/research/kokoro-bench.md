# Kokoro render benchmark

This note records the render measurements from 15 November 2026 that led to the Kokoro
settings in [`kokoro_tts.py`](../../src/pitwall/audio/kokoro_tts.py). The raw results and
the scripts are in [`kokoro-bench/`](kokoro-bench/).

## Setup

- GPU: NVIDIA GeForce RTX 5070 Ti, 16 GB.
- Packages: `onnxruntime-gpu` 1.32.0 and `kokoro-onnx` 0.6.1.
- Model: `kokoro-v1.0.onnx` (fp32, 325 MB). One comparison used `kokoro-v1.0.fp16.onnx` (163 MB).
- Lines: the 24 radio lines in `lines.json`, rendered in 3 passes, so each result has 72 renders.
- Time is wall time for one render. RTF is render time divided by audio time.

The CPU model of the test PC was not recorded.

## Engine comparison

`bench.py` wrote these results. The voice is `bf_emma` unless the label names another voice.

| Result file | Engine | First render | p50 | p95 | Max | RTF | VRAM change |
|---|---|---|---|---|---|---|---|
| `result-piper.json` | Piper on the CPU | 1105 ms | 415 ms | 564 ms | 590 ms | 0.141 | 27 MiB |
| `result-kokoro-cpu.json` | Kokoro on the CPU | 1507 ms | 589 ms | 734 ms | 762 ms | 0.212 | none |
| `result-kokoro-gpu.json` | Kokoro on CUDA, session built by `kokoro-onnx` | 10232 ms | 123 ms | 177 ms | 184 ms | 0.048 | 1586 MiB |
| `result-kokoro-gpu-algo.json` | Kokoro on CUDA, `cudnn_conv_algo_search=DEFAULT` | 886 ms | 160 ms | 210 ms | 215 ms | 0.062 | 1545 MiB |
| `result-kokoro-gpu-fp32-rerun.json` | Same as the row above, second run | 754 ms | 172 ms | 232 ms | 250 ms | 0.067 | 1381 MiB |
| `result-kokoro-gpu-bf_emma-bench.json` | Same as the row above, third run | 738 ms | 156 ms | 205 ms | 208 ms | 0.060 | 1523 MiB |
| `result-kokoro-gpu-af_heart-bench.json` | Same settings, voice `af_heart` | 754 ms | 163 ms | 208 ms | 221 ms | 0.060 | 1579 MiB |

Findings:

- Kokoro on CUDA renders a line in about 160 ms at p50. Kokoro on the CPU needs 589 ms and Piper needs 415 ms.
- The session that `kokoro-onnx` builds took 10.2 s for its first render. With `cudnn_conv_algo_search=DEFAULT` the first render took less than 0.9 s.
- The `DEFAULT` search costs about 35 ms at p50 against the `kokoro-onnx` session.
- Kokoro on CUDA holds about 1.5 GB of GPU memory.
- The `af_heart` and `bf_emma` voices render at the same speed.

## The fp16 model returns NaN audio on CUDA

`sweep.py` rendered 404 fixed phrases (`corpus.json`) at speeds 0.95, 1.0 and 1.1, which gives 1212 cases.

| Model | Provider | Bad cases | Source |
|---|---|---|---|
| `kokoro-v1.0.fp16.onnx` | CUDA | 47 of 1212, all samples NaN, 27 different phrases | `sweep-kokoro-v1.0.fp16.onnx-DEFAULT-all.json` |
| `kokoro-v1.0.onnx` | CUDA | 0 of 1212 | `sweep-kokoro-v1.0.onnx-DEFAULT-all.json` |

`diag.py` gave the same answer on the 24 lines plus two radio checks:

| Model | Provider | Clean renders | Source |
|---|---|---|---|
| `kokoro-v1.0.fp16.onnx` | CUDA | 50 of 52 | `diag-kokoro-v1.0.fp16.onnx-cuda.json` |
| `kokoro-v1.0.fp16.onnx` | CPU | 26 of 26 | `diag-kokoro-v1.0.fp16.onnx-cpu.json` |
| `kokoro-v1.0.onnx` | CUDA | 52 of 52 | `diag-kokoro-v1.0.onnx-cuda.json` |

Pitwall therefore uses the fp32 model. Do not switch to the fp16 model on the GPU.

## Session options

`drive.py` ran one option at a time through `x.py`, with a baseline run before and after each
option. The voice is `af_heart` and the baseline uses `cudnn_conv_algo_search=DEFAULT`.

The 14 baseline runs (`result-x-q3-base*.json`) had a p50 from 139.6 to 147.7 ms, a p95 from
183.1 to 202.7 ms and a VRAM change from 1537 to 1573 MiB. A difference inside these ranges
is noise.

| Result file | Option | p50 | p95 | VRAM change |
|---|---|---|---|---|
| `result-x-heur.json` | `cudnn_conv_algo_search=HEURISTIC` | 133.3 ms | 171.3 ms | 1548 MiB |
| `result-x-exh.json` | `cudnn_conv_algo_search=EXHAUSTIVE` | 133.6 ms | 170.1 ms | 1549 MiB |
| `result-x-arena.json` | `arena_extend_strategy=kSameAsRequested` | 147.1 ms | 202.3 ms | 1423 MiB |
| `result-x-maxws0.json` | `cudnn_conv_use_max_workspace=0` | 140.4 ms | 184.2 ms | 1549 MiB |
| `result-x-tf32off.json` | `use_tf32=0` | 142.0 ms | 183.8 ms | 1550 MiB |
| `result-x-nc1d.json` | `cudnn_conv1d_pad_to_nc1d=1` | 146.2 ms | 190.2 ms | 1549 MiB |
| `result-x-intra1.json` | 1 intra-op thread | 144.0 ms | 187.3 ms | 1549 MiB |
| `result-x-intra2.json` | 2 intra-op threads | 143.2 ms | 185.3 ms | 1548 MiB |
| `result-x-iobind.json` | IO binding | 148.2 ms | 204.5 ms | 1601 MiB |
| `result-x-optsave.json` | Save the optimised graph | 141.7 ms | 194.0 ms | 1548 MiB |
| `result-x-optload.json` | Load the optimised graph | 142.3 ms | 195.4 ms | 1553 MiB |
| `result-x-phcache.json` | Cache phonemes | 143.1 ms | 188.9 ms | 1549 MiB |
| `result-x-prof.json` | Profiling on | 181.7 ms | 228.8 ms | 1549 MiB |

Findings:

- `HEURISTIC` and `EXHAUSTIVE` search are about 10 ms faster at p50 than the baseline. No other option is outside the baseline range.
- `arena_extend_strategy=kSameAsRequested` used about 125 MiB less GPU memory than the baseline median of 1549 MiB.
- No option produced NaN audio.
- Phoneme conversion takes 0.2 ms at p50, so a phoneme cache saves nothing.
- `do_copy_in_default_stream=0` is in `drive.py` but has no result file.

## Files

- `bench.py` compares engines. `x.py` runs one option and `drive.py` runs the option set.
- `sweep.py` and `diag.py` look for NaN audio. `make_corpus.py` and `make_lines.py` write the inputs.
- `q2.py` counts phrase-cache hits in a decision log. Its output was not saved.
- `ph.py`, `warn.py`, `sample_heart.py` and `profsum.py` are small helpers.

The scripts are a record, not a maintained tool. `make_corpus.py` and `q2.py` contain absolute
paths from the test PC. The scripts expect the model files and `voices-v1.0.bin` next to them.
The sample WAV files and the model files are not in the repo.
