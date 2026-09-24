"""GLM 5.3 Flash, one provider at a time: speed, reasoning-cap discipline,
cache hits, JSON mode. Uses the production llm._request (streaming, hard
deadline) with fallbacks off, on the latency bench's real chat prompts. Setup as in latency_bench.py.

Usage:  AURA_DB_PATH=<copy> BENCH_DIR=<dir> python bench/provider_bench.py
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = [sys.argv[0]]
import latency_bench as lb   # stubs client, builds prompts from the DB copy
import llm, memory, sqlite3

PROVIDERS = ["z-ai", "gmicloud", "novita", "sail-research", "fireworks", "together", "siliconflow", "parasail"]
conn = sqlite3.connect(f"file:{memory.DB_PATH}?mode=ro", uri=True)
size, rid, lb.T = lb.pick_message(conn)
msg = conn.execute("SELECT message FROM operator_dialogue WHERE id = ?", (rid,)).fetchone()[0]
prompts = {}
for name, kw in {"8 turns": dict(turns=8), "cap 80k": dict(cap=80_000), "cap 150k": dict(cap=150_000)}.items():
    ctx, n, span = lb.timeline(conn, rid, lb.T, **kw)
    prompts[name] = lb.build_prompt(ctx, msg)

JSON_PROMPT = 'Respond ONLY in valid JSON: {"title": "a five word title about scarcity", "n": 3}'
out = open(os.path.join(lb.HERE, "provider_results.jsonl"), "a")
llm.PROVIDER_FALLBACKS = False
llm.PROVIDER_IGNORE = []

def run(prov, label, prompt, json_mode, reasoning):
    llm.PROVIDER_ORDER = [prov]
    t0 = time.monotonic()
    rec = {"provider_req": prov, "window": label}
    try:
        c = llm._request([{"role": "user", "content": prompt}], 0.7, json_mode,
                         timeout=180, reasoning_tokens=reasoning)
        u = c.usage
        rec.update(ok=True, served=c.provider, finish=c.finish_reason, prompt_tok=u.get("prompt_tokens"),
                   cached_tok=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                   reasoning_tok=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                   completion_tok=u.get("completion_tokens"), cost=u.get("cost"), reply_chars=len(c.text))
    except Exception as e:
        rec.update(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
    rec["secs"] = round(time.monotonic() - t0, 1)
    rec["at"] = time.strftime("%H:%M:%S", time.gmtime())
    out.write(json.dumps(rec) + "\n"); out.flush()
    print(json.dumps(rec), flush=True)

for rnd in (1, 2):
    for prov in PROVIDERS:
        if rnd == 1:
            run(prov, "json", JSON_PROMPT, True, 4000)
        for label, prompt in prompts.items():
            run(prov, label, prompt, False, 4000)
