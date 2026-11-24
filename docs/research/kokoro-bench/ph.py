from kokoro_onnx import Kokoro
k = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
for t in ["1:28.432", "1:04.119", "+0.842s", "-1.20s", "DRS", "D R S", "ERS", "MGU-K", "P4", "T1", "S2", "C3", "14L softs",
          "Gap to Leclerc 1.4 seconds", "Gap to [ʃɑːl ləklˈɛə] ahead", "VSC, VSC. Stay on the delta", "P1 and in clean air"]:
    print(f"{t!r:40} -> {k.tokenizer.phonemize(t, 'en-gb')}")
