import json, sys, collections, glob
f = sorted(glob.glob("prof-prof_*.json"))[-1]
ev = json.load(open(f))
nodes = [e for e in ev if e.get("cat") == "Node" and e["name"].endswith("_kernel_time")]
runs = [e for e in ev if e.get("cat") == "Session" and e["name"] == "model_run"]
print("file", f, "runs", len(runs), "mean model_run ms", sum(e["dur"] for e in runs) / len(runs) / 1000)
n = len(runs)
byprov = collections.Counter(); cnt = collections.Counter(); byop = collections.Counter(); opn = collections.Counter()
for e in nodes:
    a = e["args"]; p = a.get("provider", "?")
    byprov[p] += e["dur"]; cnt[p] += 1
    byop[(p, a.get("op_name"))] += e["dur"]; opn[(p, a.get("op_name"))] += 1
tot = sum(byprov.values())
print("kernel time per run ms:", {p: round(v / n / 1000, 1) for p, v in byprov.items()}, "node execs per run:", {p: round(c / n) for p, c in cnt.items()})
print("sum kernel per run ms", round(tot / n / 1000, 1))
for (p, op), v in byop.most_common(25):
    print(f"  {p:24s} {op:22s} {v / n / 1000:7.2f} ms/run  {opn[(p, op)] / n:7.1f} execs/run")
other = [e for e in ev if e.get("cat") != "Node"]
c = collections.Counter()
for e in other: c[e["name"]] += e["dur"]
print({k: round(v / n / 1000, 1) for k, v in c.most_common(8)})
