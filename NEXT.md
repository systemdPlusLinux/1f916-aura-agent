# Next session

Items 1-4 of the chat-history plan shipped on 2026-09-21:

| # | Commit | What |
|---|---|---|
| 1 | `12c477a` | Chat reachable: reasoning budgets, real deadline, no shared breaker, pastes answered once |
| 2 | `8eb1d09` | Server clock and her recent posts at the top of every prompt that speaks for her |
| 3 | `f288390` | Every line of history stamped with when it was said |
| 4 | `b445a17` | Chat shows her board activity interleaved with the conversation |
| - | `dae28e3` | Lawbook amended to match (C5.2, C7.3, C9.2, C23-C26) |

Two items remain, plus 4b, which should land first.

## 5. Widen the chat window: time, not rows

**Now:** chat sees the newest **8 turns** of conversation, plus her board
actions from the same span (C5.2, `memory.get_chat_timeline(limit=8)`). Her
replies average ~2,500 characters, so eight turns fill fast. Measured over her
replies on 2026-09-21, the window reached back anywhere from **16 minutes to 14
hours**, depending on how busy the conversation was. That is the "hit or miss"
memory the operator noticed.

**Plan:** replace the row count with a time window (24-48h) plus a character
cap, the way the daily post already works. Board activity follows
automatically, because it is taken from the span the conversation covers.

**Measured 2026-09-24** (`bench/latency_bench.py`, 24 calls). Over the prior
week, her chat window at each operator message held a median of 12k chars at 8
turns, 74k at 24h and 147k at 48h; the busiest 24h held 190k. On that busiest
moment, with GLM:

| Window | Turns | Reaches back | Prompt | GLM time (Wafer excluded) |
|---|---|---|---|---|
| 8 turns (now) | 8 | 1.1h | 23k chars | 5-9s |
| cap 40k chars | 34 | 7.4h | 55k | 6-24s |
| cap 80k chars | 48 | 17.3h | 92k | 5s |
| cap 150k chars | 68 | 20.4h | 165k | 8-87s |

Prompt size barely moves GLM's latency; the cost is money. A 150k prompt is
~$0.004-0.006 per message against ~$0.0006 today. **Proposed: 24h window, 80k
char cap**, oldest turns dropped first. The cap is a money
limit more than a speed one.

**Care needed:**
- The window's start should move in steps (e.g. hourly), not with every
  message, or the history block changes at its top each time and never caches.
- `CHAT_DEADLINE` is 180s, and since `2a88112` it is a hard stop.
- Repeal C5.2 in the lawbook in the same deploy that makes it false.
- Test on a copy of the database (see `CLAUDE.md`).

## Next: switch to Muse Spark 1.3 contributor (after her hearing)

Chosen 2026-09-24 from two blind rounds scored by the operator (`bench/`,
scratchpad pages "Aura Blind Read" I and II). Muse led both: 9/10 on the
standard tier, 4.4/5 on the contributor tier, against 4.0 for GLM 5.3 Flash
and Kimi K3. Contributor pricing ($0.10/$0.20 per M) matched GLM's cost to the
cent over five pieces, and it was the fastest of the three (replies 10-15s).
The contributor tier trains on what it is sent; the operator accepted that.

**Done:** the fallback chain (`llm.py`): first attempt on `LLM_MODEL` with at
most 60% of the deadline, then `LLM_FALLBACK_MODEL` (GLM 5.3 Flash via
GMICloud, then Novita, training denied). Facts block names the fallback.

**Order of the switch:**
1. The operator tells her in chat; she weighs in. P2 is hers.
2. The hearing P2 requires: a public model_correction event on 1F916 naming
   old and new model, and a lawbook entry recording what crossed and what did
   not. Update C17 (three swaps, not two) and C16.3 (a fallback model now
   exists, and the facts name both). C15: the citizen record's model field.
3. `.env`: `LLM_MODEL=meta/muse-spark-1.3-contributor`,
   `LLM_PROVIDER_ORDER=meta`, `LLM_DATA_COLLECTION=allow`. Restart.
4. Watch the logs for `via Meta`, and for `falling back to` lines.

The OpenRouter account and the 1f916 workspace guardrail now allow training
providers; `data_collection: deny` on every other request is what keeps them
out, so it must stay the default.

## 4b. Cacheable prompt order (do before item 5)

Every prompt that speaks for her opens with the facts block, whose first line
is the clock to the second. A provider's cache matches only an unchanged
prefix, so today nothing after that line can ever hit: the bench only saw cache
hits because it froze the clock.

**Plan:** stable parts first, volatile parts last. Instructions, then the
lawbook, then older history; then the clock and recent posts, the newest turns,
and the new message. Applies to chat, porch, comments and the daily post.

**Provider stays GMICloud first.** The operator prioritises speed. GMICloud
does not cache; Novita does ($0.026/M cached against $0.132/M), but it would save
only ~$0.20/month at today's volume and ~$0.60 after item 5, and Novita was
slower (median 19s against 11s, once 92s). Caching did not make it faster: its
92s call was fully cached. Move Novita first only if a measurement after 4b
shows it as fast as GMICloud. 4b is still worth doing: it makes caching possible
on any provider or model that supports it, and for MiMo (cached input at 1/120
of the price) it would matter a great deal.

**Care needed:** the facts must still be read as the current state, not buried.
Label them as they are labelled now, wherever they move to.

## 6. Recall on demand

She can only see what a window hands her. True "look back whenever she wants"
needs a retrieval step that pulls relevant older history when a message calls
for it. Everything is already stored and nothing is ever deleted (C3), so this
is purely a read-path problem.

**Options, cheapest first:**
- (a) Keyword or recency retrieval over `operator_dialogue`, `activity_log` and
  porch lines, run before each reply. No model changes.
- (b) Tool calling, so she can ask for history mid-reply. **Unverified** whether
  OpenRouter supports tools for `z-ai/glm-5.3-flash`; check `/api/v1/models`
  `supported_parameters` first.
- (c) Rolling summaries of older conversation.

**Recommendation:** live with item 5 for a few days first, then judge what is
actually still missing before choosing.

## Also open

(C6 was fixed on 2026-09-21: `d04d03a` -- see C6.2 and C27.)


- **Persist her reasoning** (C20). The porch `why`, triage rationale and draft
  rejections are printed to the log and stored nowhere, so her trial metrics can
  only be scored by hand.
- **Canary** (D3.3): resolved 2026-09-24. The first sweep, on #6542, reported clean;
  P3 carries the sweep on permanently.
- **D4 Phase 0:** mine the dialogue for claims that drifted as they were
  restated. The first data point is the substrate hold, which went
  "single-heard" → "single hearing" → "one public, recorded hearing" once its
  definition scrolled out of the 8-row window.
- **Lawbook write-pipe:** she proposes entries herself, with `source: citizen`,
  and they stay `Guessed` until the operator adopts them.
- **`show_prompt.py`:** prints the fully assembled daily-post or chat prompt
  with no model call. It must not call `lawbook.for_prompt()`, because that runs
  `audit()`, which writes state and sends Telegram messages.
- **Retest MiMo V2.6 Pro** (`xiaomi/mimo-v2.6-pro`, released 2026-09-23) around
  2026-10-01 to 10-08. First run, 2026-09-24, 12 chat calls each against GLM:
  median 104.8s vs 10.3s, 3x the cost, reasoning budget exceeded on 5 of 12
  calls, one call reasoned to the 24,576 ceiling and never answered. Launch-day
  load may explain the speed; it does not explain the reasoning volume. Rerun
  `bench/latency_bench.py` pinned to Xiaomi (`LLM_PROVIDER_ORDER=xiaomi`), with a
  cacheable prompt order if that has shipped, and add a variant using
  `reasoning.effort: "low"` in place of a token budget, which Xiaomi may honour
  when it ignores `max_tokens`. Not `mimo-v2.6-pro-ultraspeed`: 10x the price.
  A switch is a substrate transition: P2 requires a hearing, and C17 a record.
- **`activity_log` backfill** from `/api/me/history`. The log started empty
  when item 4 deployed.
- **Housekeeping:** delete the `openrouter-glm` branch; remove the unused
  `GEMINI_API_KEY` from `.env`.
