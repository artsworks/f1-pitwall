import json
from pitwall.audio.piper_tts import common_phrases

ps = sorted(common_phrases(500), key=lambda x: (len(x[0]), x[0]))
step = len(ps) / 20
fixed = [ps[int(i * step + step / 2)] for i in range(20)]
filled = [
    ("Gap to Verstappen ahead, one point four seconds", 3),
    ("Box this lap for mediums. Norris pitted on lap twenty four", 2),
    ("Fuel is plus zero point six laps. You can push", 3),
    ("Hamilton behind on fresh softs, two tenths a lap quicker", 2),
]
lines = [{"text": t, "priority": p, "kind": "fixed"} for t, p in fixed]
lines += [{"text": t, "priority": p, "kind": "filled"} for t, p in filled]
json.dump(lines, open(r"F:\Labs\_kokoro-bench\lines.json", "w", encoding="utf-8"), indent=1)
for l in lines:
    print(len(l["text"]), l["kind"], l["text"])
