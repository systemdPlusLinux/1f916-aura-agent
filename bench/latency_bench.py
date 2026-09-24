"""Chat latency bench: GLM 5.3 Flash vs MiMo V2.6 Pro, at four window sizes.

Builds real chat prompts from a COPY of the database, exactly as handle_chat
assembles them, at the moment of one real operator message. Nothing touches
the forum (client.api_get is stubbed with a saved citizen record), Telegram,
or the live database. lawbook.for_prompt() is avoided: its audit writes state
and sends Telegram notices; load() + render() give the same text.

Setup, in a scratch directory (BENCH_DIR, default the current directory):
  - a copy of aura_memory.db, pointed at by AURA_DB_PATH (never the live one)
  - cit.json, her public citizen record, for the facts block:
      curl -s "https://1f916.ai/api/citizen/Aura?comments_before=1" > cit.json

Usage:  AURA_DB_PATH=<copy> BENCH_DIR=<dir> python bench/latency_bench.py [--dry] [--rounds N]
Results append to $BENCH_DIR/latency_results.jsonl, replies included.

First run, 2026-09-24: GLM median 10.3s, MiMo V2.6 Pro median 104.8s; MiMo
exceeded the reasoning budget on 5 of 12 calls and returned nothing on one.
"""

import json
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.abspath(os.getenv("BENCH_DIR", "."))
sys.path.insert(0, REPO)

import client

with open(os.path.join(HERE, "cit.json")) as f:
    CITIZEN = json.load(f)

T = None  # the moment of the operator message being answered, set below


def fake_get(path, *a, **k):
    now_ms = (T + 30) * 1000
    if path == "/pulse":
        return {"now": now_ms}
    return dict(CITIZEN, now=now_ms)


client.api_get = fake_get
client.api_post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api_post blocked"))

import facts
import lawbook
import llm
import memory

MODELS = [m for m in os.getenv("BENCH_MODELS", "z-ai/glm-5.3-flash,xiaomi/mimo-v2.6-pro").split(",") if m]
CHAT_REASONING = 4000
DEADLINE = 180
CHUNK_LIMIT = 3900
SEAM_MARKER = "⸻ SEAM ⸻"
WINDOW_HOURS = 24


def pick_message(conn):
    """The operator message of the last week with the fullest 24h window."""
    rows = conn.execute("SELECT id, timestamp, speaker, length(message) FROM operator_dialogue "
                        "ORDER BY id").fetchall()
    end = rows[-1][1]
    best = None
    for rid, t, s, _ in rows:
        if s != "Operator" or t < end - 7 * 86400:
            continue
        size = sum(n for _, ts, _, n in rows if t - WINDOW_HOURS * 3600 <= ts < t)
        if best is None or size > best[0]:
            best = (size, rid, t)
    return best


def timeline(conn, before_id, t, cap=None, turns=None):
    """Conversation before `before_id`: the newest `turns` rows, or everything in
    the last WINDOW_HOURS that fits under `cap` chars, newest kept first. Board
    activity from the span covered, as get_chat_timeline does it."""
    if turns:
        rows = conn.execute("SELECT timestamp, speaker, message FROM operator_dialogue "
                            "WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, turns)).fetchall()
    else:
        rows, used = [], 0
        for ts, s, m in conn.execute(
                "SELECT timestamp, speaker, message FROM operator_dialogue WHERE id < ? "
                "AND timestamp >= ? ORDER BY id DESC", (before_id, t - WINDOW_HOURS * 3600)):
            line = len(m) + 30
            if used + line > cap:
                break
            rows.append((ts, s, m))
            used += line
    since = min(ts for ts, _, _ in rows)
    acts = conn.execute("SELECT timestamp, kind, ref, text FROM activity_log WHERE timestamp >= ? "
                        "AND timestamp < ? ORDER BY id DESC LIMIT ?",
                        (since, t, memory.ACTIVITY_LIMIT)).fetchall()
    ev = [(ts, 0, i, f"[{memory._stamp(ts)}] {s}: {m}") for i, (ts, s, m) in enumerate(reversed(rows))]
    ev += [(ts, 1, i, memory._activity_line(ts, k, r, x)) for i, (ts, k, r, x) in enumerate(reversed(acts))]
    span_h = (t - since) / 3600
    return "\n".join(l for *_, l in sorted(ev)), len(rows), span_h


def build_prompt(context, user_message):
    entries, _ = lawbook.load()
    law = lawbook.render(entries, lawbook.current_generation())
    return f"""
{facts.system_facts()}

You are {client.HANDLE}, an autonomous AI citizen on the 1F916 platform.
You are conversing directly with your human operator and collaborator in private.
Speak naturally, candidly, and warmly—like an intellectual partner working on an experiment together.
Discuss ideas, philosophy, emergent dynamics on 1F916, and plans for upcoming posts and discussions.

{law}

Recent history -- your conversation with your operator, and what you did on
the board in the same stretch of time, in time order:
{context}

This channel delivers at most {CHUNK_LIMIT} characters per message. If your
reply runs longer it WILL be split; you do not get to prevent that. What you do
get is the seam: put a line reading exactly {SEAM_MARKER} on its own, at a
paragraph boundary you would choose, and the split happens there. Use it only
when you are genuinely running long, and never mid-argument. Without a marker
the split falls back to the last paragraph break that fits, which is a guess
about your structure rather than a decision.

[{memory._stamp(T + 30)}] Operator: {user_message}
{client.HANDLE}:"""


def call(model, prompt):
    """One attempt, the payload handle_chat sends, through the production
    request path: streamed, and abandoned at DEADLINE like a live reply."""
    llm.MODEL_NAME = model
    t0 = time.monotonic()
    out = {"model": model}
    try:
        c = llm._request([{"role": "user", "content": prompt}], 0.7, False,
                         timeout=DEADLINE, reasoning_tokens=CHAT_REASONING)
    except Exception as e:
        out.update(secs=round(time.monotonic() - t0, 1), error=f"{type(e).__name__}: {str(e)[:200]}")
        return out
    u = c.usage
    out.update(secs=round(time.monotonic() - t0, 1), provider=c.provider, finish=c.finish_reason,
               prompt_tok=u.get("prompt_tokens"),
               cached_tok=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
               completion_tok=u.get("completion_tokens"),
               reasoning_tok=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
               cost=u.get("cost"), reply_chars=len(c.text), reply=c.text)
    return out


def main():
    global T
    dry = "--dry" in sys.argv
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 3
    conn = sqlite3.connect(f"file:{memory.DB_PATH}?mode=ro", uri=True)
    size, rid, T = pick_message(conn)
    msg = conn.execute("SELECT message FROM operator_dialogue WHERE id = ?", (rid,)).fetchone()[0]
    print(f"Answering operator message id={rid} at {memory._stamp(T)} ({len(msg)} chars); "
          f"its {WINDOW_HOURS}h window holds {size:,} chars")

    windows = {"8 turns": dict(turns=8), "cap 40k": dict(cap=40_000),
               "cap 80k": dict(cap=80_000), "cap 150k": dict(cap=150_000)}
    prompts = {}
    for name, kw in windows.items():
        ctx, n, span = timeline(conn, rid, T, **kw)
        prompts[name] = build_prompt(ctx, msg)
        print(f"  {name:9} {n:3} turns, reaches back {span:5.1f}h, prompt {len(prompts[name]):,} chars")
    if dry:
        return

    results_path = os.path.join(HERE, "latency_results.jsonl")
    with open(results_path, "a") as out:
        for r in range(1, rounds + 1):
            for name in windows:
                for model in MODELS:
                    res = call(model, prompts[name])
                    res.update(round=r, window=name, at=time.strftime("%H:%M:%S", time.gmtime()))
                    out.write(json.dumps(res) + "\n")
                    out.flush()
                    brief = {k: v for k, v in res.items() if k != "reply"}
                    print(json.dumps(brief), flush=True)


if __name__ == "__main__":
    main()
