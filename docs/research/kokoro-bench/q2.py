import collections, json
from pitwall.audio.piper_tts import common_phrases
from pitwall.audio.speaker import STARTUP_LINES
from pitwall.config.loader import ConfigStore

settings = ConfigStore().current()
speech = settings.speech
extra = [(r, 1) for r in (*settings.input.ack_replies, *settings.input.neg_replies, *settings.input.bookmark_replies)]
if settings.menu.speak_on_scroll:
    extra.extend((i.label, 1) for i in settings.menu.items)
warm = set(common_phrases(speech.warm_phrases, extra))
full = set(common_phrases(10**9, extra))
print("engine", speech.engine, "warm_phrases", speech.warm_phrases, "warm set", len(warm), "all fixed", len(full),
      "by priority", sorted(collections.Counter(p for _, p in full).items()))
full_text = {t for t, _ in full}
rows = [json.loads(l) for l in open(r"F:\Labs\f1-pitwall\recordings\balanced.decisions.jsonl", encoding="utf-8")]
fired = [r for r in rows if r["outcome"] == "fired" and r.get("text")]
c = collections.Counter(); seen = set(); miss_rules = collections.Counter(); prio_mismatch = collections.Counter()
for r in fired:
    key = (r["text"], r["priority"])
    if key in warm: k = "hit: in warm set (limit %d)" % speech.warm_phrases
    elif key in full: k = "miss: fixed phrase beyond warm limit"
    elif r["text"] in full_text: k = "miss: fixed text, other priority"; prio_mismatch[(r["rule_id"], r["priority"])] += 1
    elif key in seen: k = "hit: filled, repeated exactly (runtime cache)"
    else: k = "miss: filled or unlisted"; miss_rules[r["rule_id"]] += 1
    seen.add(key); c[k] += 1
print("fired calls with text:", len(fired))
for k, n in c.most_common(): print(f"  {n:4d} {n/len(fired):6.1%}  {k}")
print("top miss rules:", miss_rules.most_common(15))
print("priority mismatch:", prio_mismatch.most_common(10))
for p in (1, 2, 3):
    f = [r for r in fired if r["priority"] == p]
    m = [r for r in f if (r["text"], p) not in warm]
    print(f"P{p}: {len(f)} calls, {len(m)} not in warm set ({len(m)/max(1,len(f)):.0%})")
ex = [r["text"] for r in fired if r["text"] not in full_text][:12]
print("examples:", ex)
