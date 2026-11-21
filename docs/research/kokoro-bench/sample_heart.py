"""Render one listening sample with af_heart on CUDA (fp32 model)."""
import wave
from pathlib import Path

import numpy as np
import onnxruntime as rt

HERE = Path(__file__).parent

rt.preload_dlls()
from kokoro_onnx import Kokoro  # noqa: E402

session = rt.InferenceSession(
    str(HERE / "kokoro-v1.0.onnx"),
    providers=[
        ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}),
        "CPUExecutionProvider",
    ],
)
print("providers:", session.get_providers())
kokoro = Kokoro.from_session(session, str(HERE / "voices-v1.0.bin"))
s, r = kokoro.create(
    "Box this lap for mediums. Norris pitted on lap twenty four",
    voice="af_heart",
    speed=1.0,
    lang="en-us",
)
out = HERE / "sample-kokoro-gpu-af_heart.wav"
with wave.open(str(out), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(r)
    w.writeframes((np.clip(s, -1, 1) * 32767).astype("<i2").tobytes())
print("file:", out, out.stat().st_size, "bytes")
print("rate:", r, "samples:", len(s), "seconds:", round(len(s) / r, 2))
print("peak abs:", float(np.max(np.abs(s))))
print("nan count:", int(np.isnan(s).sum()))
