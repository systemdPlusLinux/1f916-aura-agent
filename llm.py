"""Shared LLM access over OpenRouter, with a bounded retry budget and a breaker.

The reasoning model is reached through OpenRouter's OpenAI-compatible chat
completions endpoint rather than a vendor SDK. That is deliberate: `requests`
is already a pinned dependency for the 1F916 API, so this path adds nothing to
the image and removes the google-genai SDK that used to be here. Switching
models is an env var (LLM_MODEL), not a code change or a rebuild.

Two properties the callers depend on, unchanged from the Gemini implementation:

  1. Retries are bounded by a wall-clock DEADLINE, not an attempt count, so a
     caller knows the worst case up front. Both callers run on threads that
     must not stall -- one is the scheduler that fires the daily post, the
     other is the Telegram listener.
  2. A circuit breaker trips after repeated exhaustions, making every later
     call in the same spark fail instantly instead of each paying the full
     deadline. It resets when a call succeeds, or explicitly at spark start.

The return value carries `.text`, which is what every call site reads.
"""

import json
import os
import re
import time

import requests
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
API_BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# The model is configuration, not code. Verified alternatives, all of which
# accept the same request shape: google/gemini-3.8-flash,
# deepseek/deepseek-v4.1-flash, openai/gpt-5.6-luna.
#
# Not "-flashx" (a pricier variant), not a "latest" alias (it changes model
# underneath you), and nothing ending ":batch" (a different delivery contract).
MODEL_NAME = os.getenv("LLM_MODEL", "z-ai/glm-5.3-flash")

# Reasoning tokens are billed and counted as output, so a ceiling sized for the
# visible answer alone truncates mid-thought -- and the gap is not small.
# Measured on a real daily post through z-ai/glm-5.3-flash: 5289 completion
# tokens for a 3287-character post, of which 4593 were reasoning. The visible
# answer was 13% of what the ceiling had to cover.
#
# 8192 looked generous against a ~1.2k-token post and was in fact 65% consumed
# on an ordinary draft, so a harder prompt would have truncated. Unused tokens
# are not billed, so headroom here costs nothing but bounds the runaway case;
# GLM 5.3 Flash permits up to 131072.
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "24576"))

# A ceiling on reasoning itself, below MAX_TOKENS so an answer always has room.
# Without one, a prompt can send the model into a reasoning runaway that never
# concludes. Reproduced on a real chat prompt on 2026-09-21 (5,543 prompt
# tokens): three attempts, each spending the entire 24,576-token budget on
# reasoning -- completion=24576, reasoning=24576, visible reply empty -- for
# 667 seconds and $0.039, and no answer at all. No deadline fixes that; only a
# reasoning budget forces the model to stop thinking and answer. A normal daily
# post measured 4,593 reasoning tokens, so this default binds only runaways.
REASONING_MAX_TOKENS = int(os.getenv("LLM_REASONING_MAX_TOKENS", "12000"))

# How long a stream may go completely silent before the attempt is abandoned as
# hung. This is not a limit on how long a reply may take -- the caller's
# deadline is that. A healthy stream is never quiet for long: reasoning arrives
# as it is produced (gaps under 3s, measured), and OpenRouter sends keep-alive
# comments while a provider is still reading the prompt.
STALL_TIMEOUT = int(os.getenv("LLM_STALL_TIMEOUT", "60"))
CONNECT_TIMEOUT = 10


def _csv(name, default=""):
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


# Which OpenRouter providers may serve her. Left to itself, OpenRouter spread
# 12 GLM calls over 9 providers: every prompt cache started cold, and one
# provider (Wafer) ignored the reasoning budget both times it was picked,
# reasoning 15k and 23k tokens for 5 and 8 minutes. The same model is not the
# same service everywhere.
#
# PROVIDER_ORDER is tried first, in order; with fallbacks on, any other
# provider may still answer if all of those fail, except those in
# PROVIDER_IGNORE. require_parameters keeps out providers that would silently
# drop the reasoning budget or JSON mode rather than honour them.
#
# Chosen from 56 calls pinned one provider at a time (bench/provider_bench.py,
# 2026-09-24), all eight within the reasoning budget. GMICloud: fp8, the
# cheapest fp8 price, median 11s and never over 19s, no failures, but no prompt
# caching. Novita: fp8, the cheapest provider that caches, no failures, but
# median 19s and once 92s. Speed comes first: caching on Novita would save well
# under a dollar a month, so GMICloud stays first unless a measurement shows
# Novita as fast. No provider was reliable enough to stand alone -- Z.AI
# stalled to the deadline once and Fireworks was rate-limited upstream -- so
# fallbacks stay on.
PROVIDER_ORDER = _csv("LLM_PROVIDER_ORDER", "gmicloud,novita")
PROVIDER_FALLBACKS = os.getenv("LLM_PROVIDER_FALLBACKS", "true").strip().lower() != "false"
PROVIDER_IGNORE = _csv("LLM_PROVIDER_IGNORE", "wafer")

# Whether providers that train on what they are sent may serve her. The account
# allows them (enabled 2026-09-24 to test a contributor-tier model), and that
# setting covers every request on the key, so it is refused here per request:
# "deny" unless LLM_DATA_COLLECTION says otherwise.
DATA_COLLECTION = os.getenv("LLM_DATA_COLLECTION", "deny").strip().lower()

# A second model for when the first cannot answer: an outage, a withdrawn tier,
# a request its only provider refuses. It has its own route and data policy, and
# only comes into play when it differs from MODEL_NAME. The first attempt goes
# to MODEL_NAME; every retry after a failure goes here.
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "z-ai/glm-5.3-flash").strip()
FALLBACK_PROVIDER_ORDER = _csv("LLM_FALLBACK_PROVIDER_ORDER", "gmicloud,novita")
FALLBACK_DATA_COLLECTION = os.getenv("LLM_FALLBACK_DATA_COLLECTION", "deny").strip().lower()

# The fallback is a last resort, not a second opinion. On 2026-10-02 she wrote
# "When I fail, someone else signs my name": one bad minute from Meta was
# enough for GLM to write under her name. Now a failed call opens an outage
# instead. While it lasts, calls fail at once without reaching OpenRouter, the
# model is retried on a widening schedule (PROBE_MINUTES, then hourly) by a
# background job, and the fallback answers only once the outage has run for
# FALLBACK_AFTER_HOURS. The first success ends it.
FALLBACK_AFTER_HOURS = float(os.getenv("LLM_FALLBACK_AFTER_HOURS", "20"))
PROBE_MINUTES = [int(m) for m in _csv("LLM_PROBE_MINUTES", "5,10,30,60")]
OUTAGE_KEY = "llm_outage"
_outage_mem = {}   # used when no database is reachable (standalone runs)

# The share of a call's deadline the first model may spend while a fallback is
# waiting. Without it a slow first attempt uses the whole budget, since an
# attempt may otherwise run to the deadline, and leaves the fallback no time.
PRIMARY_SHARE = float(os.getenv("LLM_PRIMARY_SHARE", "0.6"))


def _provider_prefs(order=None, data_collection=None):
    order = PROVIDER_ORDER if order is None else order
    prefs = {"require_parameters": True,
             "data_collection": data_collection or DATA_COLLECTION}
    if order:
        prefs["order"] = order
        prefs["allow_fallbacks"] = PROVIDER_FALLBACKS
    if PROVIDER_IGNORE:
        prefs["ignore"] = PROVIDER_IGNORE
    return prefs


def _state(value=None, clear=False):
    """The outage record, kept in agent_state so every thread and restart
    sees the same one. Falls back to memory if the database is unreachable."""
    try:
        import memory   # lazily: llm is imported by modules memory never sees
        if clear:
            memory.set_state(OUTAGE_KEY, "")
        elif value is not None:
            memory.set_state(OUTAGE_KEY, json.dumps(value))
        else:
            raw = memory.get_state(OUTAGE_KEY)
            return json.loads(raw) if raw else None
    except Exception:
        if clear:
            _outage_mem.pop("o", None)
        elif value is not None:
            _outage_mem["o"] = value
        else:
            return _outage_mem.get("o")


def _notify(text):
    try:
        from telegram_bot import notify_operator   # lazily, as lawbook does
        notify_operator(text)
    except Exception as e:
        print(f"[LLM] Could not notify operator: {e}")


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def outage():
    """The open outage of MODEL_NAME, or None: {since, attempts, next, cause}."""
    return _state()


def fallback_allowed():
    o = outage()
    return bool(o) and time.time() - o["since"] >= FALLBACK_AFTER_HOURS * 3600


def probe_due():
    o = outage()
    return bool(o) and time.time() >= o["next"]


def _note_failure(cause):
    o = outage()
    now = time.time()
    if not o:
        o = {"since": now, "attempts": 1, "cause": str(cause)[:300], "fallback_noticed": False}
        print(f"[{MODEL_NAME}] outage opened: {cause}")
        _notify(f"⚠️ {MODEL_NAME} is not answering ({' '.join(str(cause).split())[:160]}). "
                f"Retrying after {', '.join(str(m) for m in PROBE_MINUTES)} minutes, then hourly; "
                f"the fallback writes only after {FALLBACK_AFTER_HOURS:g} hours.")
    else:
        o["attempts"] += 1
        o["cause"] = str(cause)[:300]
    step = o["attempts"] - 1
    o["next"] = now + 60 * (PROBE_MINUTES[step] if step < len(PROBE_MINUTES) else 60)
    _state(o)


def _note_success(model):
    if model != MODEL_NAME:
        return
    o = outage()
    if o:
        hours = (time.time() - o["since"]) / 3600
        _state(clear=True)
        print(f"[{MODEL_NAME}] outage closed after {hours:.1f}h")
        _notify(f"✅ {MODEL_NAME} is answering again, after {hours:.1f} hours.")


def probe():
    """One small call to MODEL_NAME alone, for the outage job. Returns True if
    it answered; either way the outage record is updated."""
    try:
        generate("Reply with the single word OK.", json_mode=False, deadline_seconds=60,
                 use_breaker=False, reasoning_tokens=200, _probe=True)
        return True
    except ModelUnavailable:
        return False


def _route():
    """[(model, provider prefs)] in the order they are tried."""
    chain = [(MODEL_NAME, _provider_prefs())]
    if FALLBACK_MODEL and FALLBACK_MODEL != MODEL_NAME:
        chain.append((FALLBACK_MODEL,
                      _provider_prefs(FALLBACK_PROVIDER_ORDER, FALLBACK_DATA_COLLECTION)))
    return chain

# Backoff schedule; the last value repeats until the deadline is reached.
DELAYS = [4, 8, 16, 32, 64]

# How many consecutive deadline exhaustions before we stop trying entirely.
FAILURE_LIMIT = 3

_consecutive_failures = 0

# OpenRouter's usage-accounting extension returns cost alongside token counts.
# It is an extension, not core OpenAI schema, so if a gateway ever rejects it
# this flips off for the process rather than taking her offline over a
# bookkeeping field. Token counts still come back either way.
_usage_ext = True

# A tool call written into the reply as text, in a model's own markup, instead
# of made through the API. GLM does this when told it may not call tools.
_TOOL_MARKUP = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class ModelUnavailable(Exception):
    """The model could not be reached or answered within the time budget."""


class ModelAuthError(ModelUnavailable):
    """The key was rejected. Retrying cannot fix this.

    A subclass so that callers already catching ModelUnavailable keep degrading
    gracefully -- she goes quiet rather than crashing a spark -- while the
    message still names the real cause instead of looking like an outage.
    """


class ModelCreditError(ModelUnavailable):
    """The account is out of credit. Retrying cannot fix this either."""


class Completion:
    """What a call returns. `.text` is the contract every call site reads."""

    __slots__ = ("text", "usage", "model", "finish_reason", "provider", "fallback",
                 "tool_calls", "lookups")

    def __init__(self, text, usage=None, model=None, finish_reason=None, provider=None,
                 tool_calls=None):
        self.text = text
        self.usage = usage or {}
        self.model = model
        self.finish_reason = finish_reason
        self.provider = provider
        # True when the fallback model wrote this rather than MODEL_NAME.
        self.fallback = False
        # Set on a round that asked for tools: [{"id", "name", "arguments"}].
        self.tool_calls = tool_calls or []
        # Set on the final reply: a label for each lookup made on the way.
        self.lookups = []


def reset_breaker():
    """Called at the start of each spark so a new run always gets a fresh try."""
    global _consecutive_failures
    _consecutive_failures = 0


def breaker_open():
    return _consecutive_failures >= FAILURE_LIMIT


def _unfence(text):
    """Strip a markdown code fence around a JSON body.

    response_format asks for JSON; it does not guarantee the model resists
    wrapping it in ```json anyway. Every call site does a bare json.loads on
    `.text`, so normalising here is what keeps ten of them from each growing
    their own parser.
    """
    match = _FENCE.match(text or "")
    return match.group(1) if match else text


def _log_usage(usage, provider=None, model=None):
    model = model or MODEL_NAME
    via = f" via {provider}" if provider else ""
    if not usage:
        print(f"[{model}]{via} no usage reported")
        return
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens")
    extra = f", reasoning={reasoning}" if reasoning else ""
    # Printed even when zero, since zero is informative. Muse Spark contributor
    # caches, but only after a warm-up: five repeats within a minute cached
    # nothing, while repeats minutes apart cached 99% of the prompt (38,115 of
    # 38,217 tokens) at 1/50 of the price (2026-09-24). A clock at the top of a
    # prompt defeats it; this line shows whether the prompts are hitting.
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        extra += f", cached={cached}"
    cost = usage.get("cost")
    money = f", cost=${cost:.6f}" if isinstance(cost, (int, float)) else ""
    print(f"[{model}]{via} tokens: prompt={prompt}, completion={completion}, "
          f"total={total}{extra}{money}")


def _relayed_provider(body):
    """The provider's name when an OpenRouter error was relayed from a
    provider rather than raised by OpenRouter itself, else None."""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return None
    meta = err.get("metadata") or {}
    if meta.get("provider_name") and not meta.get("is_byok"):
        return meta["provider_name"]
    return None


def _read_stream(res, stop_at):
    """Assemble a streamed completion, abandoning it the moment `stop_at` passes.

    Returns (content, finish_reason, usage, model, provider, tool_calls). Raises
    _Transient if the deadline passes, the provider reports an error mid-stream,
    or the stream ends without finishing. Tool calls arrive in pieces keyed by
    index -- the name once, the arguments as a string in fragments -- and are
    assembled here.
    """
    content, finish, usage, model, provider, done = [], None, {}, None, None, False
    calls = {}
    for raw in res.iter_lines():
        if time.monotonic() > stop_at:
            # Closing the stream (the caller's `with`) is also what tells
            # OpenRouter to stop generating, so the abandoned remainder is not
            # billed where the provider supports cancellation.
            raise _Transient("no complete reply before the deadline; stream abandoned")
        # SSE carries blank separators and `: keep-alive` comments; skip both.
        # Decoded here, as UTF-8: requests assumes Latin-1 for text/event-stream
        # without a charset, which garbles every non-ASCII character.
        line = raw.decode("utf-8", errors="replace") if raw else ""
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        # A provider failure after the 200 arrives as an error chunk, so status
        # alone is not proof of a completion.
        if chunk.get("error"):
            raise _Transient(f"provider error: {str(chunk['error'])[:200]}")
        model = chunk.get("model") or model
        provider = chunk.get("provider") or provider
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["arguments"] += fn.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    if finish == "error":
        raise _Transient("provider error mid-stream (finish_reason=error)")
    if not done and finish is None:
        raise _Transient("stream ended before the reply finished")
    tool_calls = [calls[i] for i in sorted(calls)]
    for n, call in enumerate(tool_calls):
        call["id"] = call["id"] or f"call_{int(time.time())}_{n}"
    return "".join(content), finish, usage, model, provider, tool_calls


def _request(messages, temperature, json_mode, timeout, reasoning_tokens=None,
             model=None, prefs=None, tools=None, tool_choice=None):
    """One HTTP call, which may take at most `timeout` seconds in all. Returns a
    Completion, or raises for the caller to judge.

    The reply is streamed so that `timeout` can be enforced. A requests timeout
    is not a limit on a call's length: it only fires when the socket goes quiet
    for that long. OpenRouter keeps a non-streamed call's socket alive while the
    model works, so on 2026-09-24 calls given a 180s timeout ran for 314, 468,
    634, 684 and 974 seconds. A stream is read piece by piece, and the clock is
    checked between pieces.
    """
    global _usage_ext
    payload = {
        "model": model or MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "provider": prefs or _provider_prefs(),
    }
    include_usage = _usage_ext
    if include_usage:
        payload["usage"] = {"include": True}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if reasoning_tokens:
        payload["reasoning"] = {"max_tokens": int(reasoning_tokens)}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    stop_at = time.monotonic() + timeout
    with requests.post(
        f"{API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Names this agent in the OpenRouter dashboard so spend is
            # attributable per project rather than pooled.
            "X-Title": "Aura on 1F916",
        },
        json=payload,
        stream=True,
        timeout=(CONNECT_TIMEOUT, min(STALL_TIMEOUT, max(1.0, timeout))),
    ) as res:
        if res.status_code == 401 and _relayed_provider(res.text):
            # A provider refusing OpenRouter's own credentials, relayed with
            # the provider named: that one route is down, not her key, and a
            # different model can still answer. Measured 2026-09-24: Meta
            # returned invalid_api_key for every contributor-tier call while the
            # key itself was valid and GLM answered on it. Read as a bad key,
            # it skipped the fallback and told the operator the key was wrong.
            raise _Transient(
                f"{_relayed_provider(res.text)} refused OpenRouter's request (HTTP 401, "
                f"provider-side): {res.text[:200]}")
        if res.status_code == 401:
            raise ModelAuthError(
                "OpenRouter rejected the key (401). Check OPENROUTER_API_KEY in "
                ".env -- it is read from the environment and never hardcoded."
            )
        if res.status_code == 402:
            raise ModelCreditError(
                "OpenRouter reports insufficient credit (402). Top up the account "
                f"or lower LLM_MAX_TOKENS (currently {MAX_TOKENS})."
            )
        if res.status_code == 429 or res.status_code >= 500:
            # Transient: worth waiting out inside the caller's deadline.
            raise _Transient(f"HTTP {res.status_code}: {res.text[:200]}")
        if 400 <= res.status_code < 500 and _relayed_provider(res.text):
            # Relayed from a provider, not raised by OpenRouter: Meta's
            # intermittent 400 "Provider returned error" (2026-09-25) cleared
            # on retry. Retried within the call's deadline, not taken as final.
            raise _Transient(f"HTTP {res.status_code} from {_relayed_provider(res.text)}, "
                             f"provider-side: {res.text[:200]}")
        if res.status_code != 200:
            # Only a rejection that names the usage field is about the usage
            # field. Treating every 4xx as one turned "no endpoint can serve
            # this" into a silent loss of cost reporting for the whole process.
            if (include_usage and 400 <= res.status_code < 500
                    and "usage" in res.text.lower()):
                # The one field here that is an extension rather than core schema.
                # Losing cost reporting is a far better outcome than a silent agent,
                # so drop it for the rest of the process and let the retry stand.
                _usage_ext = False
                raise _Transient(
                    f"HTTP {res.status_code} with usage accounting on; dropping it "
                    f"and retrying: {res.text[:160]}"
                )
            # Any other 4xx -- a malformed request, no endpoint that can serve
            # it, a data policy that excludes every provider -- fails the same
            # way on a retry to the same model, so it is not retried here.
            # generate() may still try the fallback model.
            raise ModelUnavailable(f"HTTP {res.status_code}: {res.text[:300]}")

        content, finish, usage, model, provider, tool_calls = _read_stream(res, stop_at)

    _log_usage(usage, provider, model or MODEL_NAME)

    if finish == "length":
        # Reasoning tokens count toward the same ceiling as the answer, so a
        # truncated body can look well-formed and end mid-sentence. Never hand
        # this back: a cut-off post is worse than no post.
        details = usage.get("completion_tokens_details") or {}
        if not content.strip() and details.get("reasoning_tokens"):
            # The whole budget went to thinking. Raising MAX_TOKENS would not
            # help -- the old advice here -- because the model never got as far
            # as answering.
            raise _Transient(
                f"reasoning ran to the ceiling ({details['reasoning_tokens']} tokens) "
                "without producing an answer"
            )
        raise _Transient(f"response truncated at max_tokens={MAX_TOKENS}")

    if tool_calls:
        # A round that asks for lookups instead of answering: the caller runs
        # them and asks again. Its text, if any, is not the reply.
        return Completion(content, usage=usage, model=model, finish_reason=finish,
                          provider=provider, tool_calls=tool_calls)

    text = _unfence(content).strip()
    if not text:
        raise _Transient(f"empty content (finish_reason={finish})")

    if json_mode:
        # Parse here so a malformed body is retried as a transient failure
        # rather than surfacing as a JSONDecodeError inside a caller that has
        # already decided what to do with the result.
        try:
            json.loads(text)
        except ValueError as e:
            raise _Transient(f"unparseable JSON ({e}): {text[:160]}")

    return Completion(text, usage=usage, model=model, finish_reason=finish,
                      provider=provider)


class _Transient(Exception):
    """Internal: a failure worth another attempt inside the deadline."""


def generate(prompt, system_instruction=None, temperature=0.7,
             deadline_seconds=180, json_mode=True, on_retry=None,
             use_breaker=True, reasoning_tokens=None,
             tools=None, tool_handlers=None, max_tool_rounds=4, describe_tool=None,
             _probe=False):
    # max_tool_rounds caps individual lookups per call, however many rounds
    # they arrive in.
    """Generate content, retrying transient failures within a time budget.

    Raises ModelUnavailable if the deadline passes or the breaker is open. Bad
    keys, exhausted credit and malformed requests raise immediately: retrying
    any of them just burns the budget.

    The deadline is real: no attempt may outlive what is left of it, and an
    attempt that is still making progress may use all of it. Before, a "180s"
    budget was only a limit on silence, so calls ran for up to 974s (see
    _request); a per-attempt cap of 120s sat under that and never fired.

    `use_breaker=False` takes the caller out of the shared circuit breaker
    entirely -- it neither trips it nor is blocked by it. That is for live
    chat. The breaker exists so a spark stops paying the full deadline on a
    model that is clearly down; a person sending a message is not a retry loop,
    and chat could only ever be un-blocked by the scheduler thread, so three
    slow replies silenced her until the next porch visit.
    """
    global _consecutive_failures

    if not OPENROUTER_API_KEY:
        raise ModelAuthError("OPENROUTER_API_KEY is not configured")

    if use_breaker and breaker_open():
        raise ModelUnavailable(
            f"{MODEL_NAME} failed {_consecutive_failures} times in a row; "
            "skipping further calls until the next spark"
        )

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    reasoning_tokens = reasoning_tokens or REASONING_MAX_TOKENS
    chain = _route()
    # The fallback is only in the chain once an outage has run its course.
    if len(chain) > 1 and (_probe or not fallback_allowed()):
        chain = chain[:1]
    o = outage()
    if o and not _probe and not fallback_allowed():
        # Waiting: the outage job retries on schedule, and every other call
        # stands down rather than adding its own retries.
        raise ModelUnavailable(
            f"{MODEL_NAME} has not answered since {_when(o['since'])}; next try "
            f"{_when(o['next'])}, the fallback only after {FALLBACK_AFTER_HOURS:g}h "
            f"(last cause: {o.get('cause', '')[:120]})")
    if o and len(chain) > 1 and not o.get("fallback_noticed"):
        o["fallback_noticed"] = True
        _state(o)
        _notify(f"↪ {MODEL_NAME} has not answered for {FALLBACK_AFTER_HOURS:g} hours. "
                f"{chain[1][0]} now writes in her place, marked as the fallback, "
                "until it recovers.")
    started = time.monotonic()
    deadline = started + deadline_seconds
    # The first model's share is of the whole call, tool rounds included, so a
    # slow run of lookups on it still leaves the fallback time to answer.
    primary_stop = started + deadline_seconds * PRIMARY_SHARE
    attempt = 0
    last_error = None
    tool_rounds = 0
    lookups = []
    limit_said = False

    while True:
        step = min(attempt, len(chain) - 1)
        model, prefs = chain[step]
        has_next = step + 1 < len(chain)
        # An attempt may use whatever budget is left, and no more -- except
        # the first model's, which leaves a share for the fallback.
        left = (primary_stop if has_next else deadline) - time.monotonic()
        attempt_timeout = max(1.0, left)
        try:
            # After the last permitted round, tools stay declared (the history
            # holds tool calls) but may not be called: she answers from what
            # she has.
            choice = "none" if tools and tool_rounds >= max_tool_rounds else None
            result = _request(messages, temperature, json_mode, timeout=attempt_timeout,
                              reasoning_tokens=reasoning_tokens, model=model, prefs=prefs,
                              tools=tools, tool_choice=choice)
            if result.tool_calls:
                if not tools or tool_rounds >= max_tool_rounds:
                    raise _Transient("asked for a lookup after the lookup limit")
                messages.append({"role": "assistant", "content": result.text or None,
                                 "tool_calls": [{"id": c["id"], "type": "function",
                                                 "function": {"name": c["name"],
                                                              "arguments": c["arguments"] or "{}"}}
                                                for c in result.tool_calls]})
                # The limit counts lookups, not rounds: one round can ask for
                # several. Calls past it are answered, not run.
                for call in result.tool_calls:
                    if tool_rounds >= max_tool_rounds:
                        output = (f"Not run: the limit of {max_tool_rounds} lookups a reply "
                                  "is reached. Answer from what you have.")
                    else:
                        tool_rounds += 1
                        output, label = _run_tool(call, tool_handlers or {}, describe_tool)
                        lookups.append(label)
                        print(f"[{model}] lookup {tool_rounds}: {label} -> {len(output)} chars")
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "name": call["name"], "content": output})
                if tool_rounds >= max_tool_rounds and not limit_said:
                    # tool_choice "none" alone was not enough: GLM answered it
                    # by writing a fifth call out as text, in its own markup,
                    # and that went out as her reply (2026-09-25).
                    limit_said = True
                    messages.append({"role": "user", "content": (
                        f"[System] That was the last of your {max_tool_rounds} lookups for this "
                        "reply. Write your reply now from what you have; do not call or write "
                        "out any more tools.")})
                continue
            if tools:
                text = _TOOL_MARKUP.sub("", result.text or "").strip()
                if not text:
                    raise _Transient("wrote a tool call out as text instead of a reply")
                result.text = text
            if use_breaker:
                _consecutive_failures = 0
            result.fallback = step > 0
            result.lookups = lookups
            if step == 0:
                _note_success(model)
            return result
        except (ModelAuthError, ModelCreditError):
            raise
        except ModelUnavailable as e:
            # This model's route cannot serve the request at all. Only a
            # different model can help.
            if not has_next:
                if step:
                    raise ModelUnavailable(
                        f"{chain[0][0]} failed ({last_error}); {model} failed ({e})") from e
                _note_failure(e)
                raise
            last_error = e
        except _Transient as e:
            last_error = e
        except requests.RequestException as e:
            # Connection resets, DNS blips, read timeouts: all worth another go.
            last_error = e

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if use_breaker:
                _consecutive_failures += 1
            tried = " then ".join(m for m, _ in chain[:step + 1])
            if step == 0:
                _note_failure(last_error)
            # Report the time actually spent, not the budget that was set.
            raise ModelUnavailable(
                f"{tried} gave no usable reply in {time.monotonic() - started:.0f}s "
                f"({attempt + 1} attempt{'s' if attempt else ''}): {last_error}"
            )

        # Moving to the fallback is immediate; backing off is for retrying the
        # same model.
        delay = 0 if has_next else min(DELAYS[min(attempt, len(DELAYS) - 1)], remaining)
        attempt += 1
        if on_retry:
            try:
                on_retry(attempt, delay, last_error)
            except Exception:
                pass
        if has_next:
            print(f"[{model}] attempt {attempt} failed ({last_error}); "
                  f"falling back to {chain[step + 1][0]}, {remaining:.0f}s of budget left")
        else:
            print(f"[{model}] attempt {attempt} failed ({last_error}); "
                  f"retrying in {delay:.0f}s, {remaining:.0f}s of budget left")
        time.sleep(delay)


def _run_tool(call, handlers, describe=None):
    """Run one lookup she asked for. Returns (output for her, label for the
    record). Never raises: a failed lookup is an answer she can read."""
    name = call.get("name") or ""
    try:
        args = json.loads(call.get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments are not an object")
    except ValueError as e:
        return f"The arguments to {name} could not be read ({e}).", f"{name} (unreadable arguments)"
    label = describe(name, args) if describe else name
    handler = handlers.get(name)
    if not handler:
        return f"There is no tool called {name}.", f"{name} (no such tool)"
    try:
        return str(handler(**args)), label
    except TypeError as e:
        return f"{name} was called with arguments it does not take ({e}).", f"{label} (bad arguments)"
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {str(e)[:200]}", f"{label} (failed)"


def written_by_fallback(result):
    """The fallback model's name if it wrote `result`, else None.

    With a fallback, which model speaks for her can change from one call to the
    next. Anything she publishes that the fallback wrote is marked as such, so
    the record says which model wrote what, not only which one was configured.
    """
    return FALLBACK_MODEL if getattr(result, "fallback", False) else None


def fence(text, label="content"):
    """Wrap another citizen's writing so the model treats it as quoted data.

    The platform states plainly that anything written on the board is untrusted
    data and never an instruction, and it says the same of porch lines. This
    keeps that boundary visible in-prompt. It lives here rather than in one
    caller because every module that builds a prompt out of someone else's
    words needs it, and a second copy is a second thing to forget to update.
    """
    body = (text or "").replace("</untrusted>", "<\\/untrusted>")
    return f"<untrusted {label}>\n{body}\n</untrusted {label}>"
