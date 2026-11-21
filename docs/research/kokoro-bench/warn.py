import json, logging, io
from kokoro_onnx import Kokoro
buf = io.StringIO(); logging.basicConfig(stream=buf, level=logging.WARNING)
k = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
lines = [l["text"] for l in json.load(open("lines.json", encoding="utf-8"))] + ["Stay on the delta", "Box box", "P1 and in clean air"]
for t in lines:
    buf.seek(0); buf.truncate()
    ph = k.tokenizer.phonemize(t, "en-gb")
    if "mismatch" in buf.getvalue():
        print(len(t.split()), "words ->", len(ph.split()), "|", t, "|", ph)
