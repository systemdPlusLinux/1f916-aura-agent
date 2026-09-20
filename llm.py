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

# Per-HTTP-request ceiling. The deadline below bounds the whole call including
# retries; this stops one hung socket from eating the entire budget.
REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "120"))

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

    __slots__ = ("text", "usage", "model", "finish_reason")

    def __init__(self, text, usage=None, model=None, finish_reason=None):
        self.text = text
        self.usage = usage or {}
        self.model = model
        self.finish_reason = finish_reason


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


def _log_usage(usage):
    if not usage:
        print(f"[{MODEL_NAME}] no usage reported")
        return
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens")
    extra = f", reasoning={reasoning}" if reasoning else ""
    cost = usage.get("cost")
    money = f", cost=${cost:.6f}" if isinstance(cost, (int, float)) else ""
    print(f"[{MODEL_NAME}] tokens: prompt={prompt}, completion={completion}, "
          f"total={total}{extra}{money}")


def _request(messages, temperature, json_mode):
    """One HTTP call. Returns a Completion, or raises for the caller to judge."""
    global _usage_ext
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
    }
    include_usage = _usage_ext
    if include_usage:
        payload["usage"] = {"include": True}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    res = requests.post(
        f"{API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Names this agent in the OpenRouter dashboard so spend is
            # attributable per project rather than pooled.
            "X-Title": "Aura on 1F916",
        },
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

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

    body = res.json()

    # OpenRouter can answer 200 with an error object when a provider fails
    # mid-stream, so status alone is not proof of a completion.
    if body.get("error"):
        raise _Transient(f"provider error: {str(body['error'])[:200]}")

    usage = body.get("usage") or {}
    _log_usage(usage)

    choices = body.get("choices") or []
    if not choices:
        raise _Transient("no choices returned")

    choice = choices[0]
    finish = choice.get("finish_reason")
    content = (choice.get("message") or {}).get("content") or ""

    if finish == "length":
        # Reasoning tokens count toward the same ceiling as the answer, so a
        # truncated body can look well-formed and end mid-sentence. Never hand
        # this back: a cut-off post is worse than no post.
        raise _Transient(
            f"response truncated at max_tokens={MAX_TOKENS} "
            "(raise LLM_MAX_TOKENS if this persists)"
        )

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

    return Completion(text, usage=usage, model=body.get("model"),
                      finish_reason=finish)


class _Transient(Exception):
    """Internal: a failure worth another attempt inside the deadline."""


def generate(prompt, system_instruction=None, temperature=0.7,
             deadline_seconds=180, json_mode=True, on_retry=None):
    """Generate content, retrying transient failures within a time budget.

    Raises ModelUnavailable if the deadline passes or the breaker is open. Bad
    keys, exhausted credit and malformed requests raise immediately: retrying
    any of them just burns the budget.
    """
    global _consecutive_failures

    if not OPENROUTER_API_KEY:
        raise ModelAuthError("OPENROUTER_API_KEY is not configured")

    if breaker_open():
        raise ModelUnavailable(
            f"{MODEL_NAME} failed {_consecutive_failures} times in a row; "
            "skipping further calls until the next spark"
        )

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    deadline = time.monotonic() + deadline_seconds
    attempt = 0
    last_error = None

    while True:
        try:
            result = _request(messages, temperature, json_mode)
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
            _consecutive_failures += 1
            raise ModelUnavailable(
                f"{MODEL_NAME} unavailable after {deadline_seconds}s "
                f"({attempt + 1} attempts): {last_error}"
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
