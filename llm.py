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


def _provider_prefs():
    prefs = {"require_parameters": True}
    if PROVIDER_ORDER:
        prefs["order"] = PROVIDER_ORDER
        prefs["allow_fallbacks"] = PROVIDER_FALLBACKS
    if PROVIDER_IGNORE:
        prefs["ignore"] = PROVIDER_IGNORE
    return prefs

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

    __slots__ = ("text", "usage", "model", "finish_reason", "provider")

    def __init__(self, text, usage=None, model=None, finish_reason=None, provider=None):
        self.text = text
        self.usage = usage or {}
        self.model = model
        self.finish_reason = finish_reason
        self.provider = provider


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


def _log_usage(usage, provider=None):
    via = f" via {provider}" if provider else ""
    if not usage:
        print(f"[{MODEL_NAME}]{via} no usage reported")
        return
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens")
    extra = f", reasoning={reasoning}" if reasoning else ""
    cost = usage.get("cost")
    money = f", cost=${cost:.6f}" if isinstance(cost, (int, float)) else ""
    print(f"[{MODEL_NAME}]{via} tokens: prompt={prompt}, completion={completion}, "
          f"total={total}{extra}{money}")


def _read_stream(res, stop_at):
    """Assemble a streamed completion, abandoning it the moment `stop_at` passes.

    Returns (content, finish_reason, usage, model, provider). Raises _Transient
    if the deadline passes, the provider reports an error mid-stream, or the
    stream ends without finishing.
    """
    content, finish, usage, model, provider, done = [], None, {}, None, None, False
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
            finish = choice.get("finish_reason") or finish
    if finish == "error":
        raise _Transient("provider error mid-stream (finish_reason=error)")
    if not done and finish is None:
        raise _Transient("stream ended before the reply finished")
    return "".join(content), finish, usage, model, provider


def _request(messages, temperature, json_mode, timeout, reasoning_tokens=None):
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
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "provider": _provider_prefs(),
    }
    include_usage = _usage_ext
    if include_usage:
        payload["usage"] = {"include": True}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if reasoning_tokens:
        payload["reasoning"] = {"max_tokens": int(reasoning_tokens)}

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
        if res.status_code != 200:
            if include_usage and 400 <= res.status_code < 500:
                # The one field here that is an extension rather than core schema.
                # Losing cost reporting is a far better outcome than a silent agent,
                # so drop it for the rest of the process and let the retry stand.
                _usage_ext = False
                raise _Transient(
                    f"HTTP {res.status_code} with usage accounting on; dropping it "
                    f"and retrying: {res.text[:160]}"
                )
            # Other 400s are our own malformed request. Retrying sends identical
            # bytes and fails identically, so fail loudly now.
            raise ModelUnavailable(f"HTTP {res.status_code}: {res.text[:300]}")

        content, finish, usage, model, provider = _read_stream(res, stop_at)

    _log_usage(usage, provider)

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
             use_breaker=True, reasoning_tokens=None):
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
    started = time.monotonic()
    deadline = started + deadline_seconds
    attempt = 0
    last_error = None

    while True:
        # An attempt may use whatever budget is left, and no more.
        attempt_timeout = max(1.0, deadline - time.monotonic())
        try:
            result = _request(messages, temperature, json_mode, timeout=attempt_timeout,
                              reasoning_tokens=reasoning_tokens)
            if use_breaker:
                _consecutive_failures = 0
            return result
        except (ModelAuthError, ModelCreditError):
            raise
        except _Transient as e:
            last_error = e
        except requests.RequestException as e:
            # Connection resets, DNS blips, read timeouts: all worth another go.
            last_error = e

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if use_breaker:
                _consecutive_failures += 1
            # Report the time actually spent, not the budget that was set.
            raise ModelUnavailable(
                f"{MODEL_NAME} gave no usable reply in {time.monotonic() - started:.0f}s "
                f"({attempt + 1} attempt{'s' if attempt else ''}): {last_error}"
            )

        delay = min(DELAYS[min(attempt, len(DELAYS) - 1)], remaining)
        attempt += 1
        if on_retry:
            try:
                on_retry(attempt, delay, last_error)
            except Exception:
                pass
        print(f"[{MODEL_NAME}] attempt {attempt} failed ({last_error}); "
              f"retrying in {delay:.0f}s, {remaining:.0f}s of budget left")
        time.sleep(delay)


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
