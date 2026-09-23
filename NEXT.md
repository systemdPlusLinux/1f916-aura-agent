# Next session

Items 1-4 of the chat-history plan shipped on 2026-09-21:

| # | Commit | What |
|---|---|---|
| 1 | `12c477a` | Chat reachable: reasoning budgets, real deadline, no shared breaker, pastes answered once |
| 2 | `8eb1d09` | Server clock and her recent posts at the top of every prompt that speaks for her |
| 3 | `f288390` | Every line of history stamped with when it was said |
| 4 | `b445a17` | Chat shows her board activity interleaved with the conversation |
| - | `dae28e3` | Lawbook amended to match (C5.2, C7.3, C9.2, C23-C26) |

Two items remain.

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

**Care needed:**
- Prompt size and latency. Reasoning budgets (C26) make latency far less
  sensitive to prompt size: a 10,335-token merged paste was answered in 19s. But
  measure a real widened chat prompt before and after. `CHAT_DEADLINE` is 180s.
- Repeal C5.2 in the lawbook in the same deploy that makes it false.
- Test on a copy of the database (see `CLAUDE.md`).

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
- **Canary** (D3.3): planted 2026-09-23. After the first sweep (the 09-24 01:30 UTC
  post), mark D3.3 `resolved` and record the verdict (`agent_state.canary_last_verdict`). P3 stays: the
  sweep itself is permanent, and its routine verdicts are not an open debt.
- **D4 Phase 0:** mine the dialogue for claims that drifted as they were
  restated. The first data point is the substrate hold, which went
  "single-heard" → "single hearing" → "one public, recorded hearing" once its
  definition scrolled out of the 8-row window.
- **Lawbook write-pipe:** she proposes entries herself, with `source: citizen`,
  and they stay `Guessed` until the operator adopts them.
- **`show_prompt.py`:** prints the fully assembled daily-post or chat prompt
  with no model call. It must not call `lawbook.for_prompt()`, because that runs
  `audit()`, which writes state and sends Telegram messages.
- **`activity_log` backfill** from `/api/me/history`. The log started empty
  when item 4 deployed.
- **Housekeeping:** delete the `openrouter-glm` branch; remove the unused
  `GEMINI_API_KEY` from `.env`.
