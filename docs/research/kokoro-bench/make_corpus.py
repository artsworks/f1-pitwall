import json
from pitwall.audio.piper_tts import common_phrases
ps = sorted(set(common_phrases(100000)))
json.dump([{"text": t, "priority": p} for t, p in ps], open(r"F:\Labs\_kokoro-bench\corpus.json", "w", encoding="utf-8"), indent=0)
print(len(ps))
