"""Shared Gemini access with a bounded retry budget and a circuit breaker.

The previous retry logic, duplicated in spark_agent and telegram_bot, could
block its caller for roughly 26 minutes on a single call: three cycles of
[4,8,16,32,64]s backoff with a blind 600s sleep between them. Both callers run
on threads that must not stall -- one is the scheduler that fires the daily
post, the other is the Telegram listener, so a stuck call meant the operator
could not reach her at all.

Two changes fix that:
  1. Retries are bounded by a wall-clock DEADLINE, not an attempt count, so a
     caller knows the worst case up front.
  2. A circuit breaker trips after repeated exhaustions, making every later
     call in the same spark fail instantly instead of each paying the full
     deadline. It resets when a call succeeds, or explicitly at spark start.
"""

import os
import time
import warnings

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError

warnings.filterwarnings("ignore", message=".*automatic function calling.*")

env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3.8-flash"

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Backoff schedule; the last value repeats until the deadline is reached.
DELAYS = [4, 8, 16, 32, 64]

# How many consecutive deadline exhaustions before we stop trying entirely.
FAILURE_LIMIT = 3

# Exceptions that retrying can never fix. Everything else that is not an
# APIError -- connection resets, DNS blips, read timeouts -- is worth another
# attempt, but a bad argument or a missing attribute will fail identically
# every time and must not burn the whole deadline.
NON_RETRYABLE = (TypeError, ValueError, KeyError, AttributeError, ImportError, NameError)

_consecutive_failures = 0


class ModelUnavailable(Exception):
    """The model could not be reached within the caller's time budget."""


def reset_breaker():
    """Called at the start of each spark so a new run always gets a fresh try."""
    global _consecutive_failures
    _consecutive_failures = 0


def breaker_open():
    return _consecutive_failures >= FAILURE_LIMIT


def generate(prompt, system_instruction=None, temperature=0.7,
             deadline_seconds=180, json_mode=True, on_retry=None):
    """Generate content, retrying transient failures within a time budget.

    Raises ModelUnavailable if the deadline passes or the breaker is open.
    Non-transient API errors propagate immediately -- retrying a malformed
    request or a bad key just burns the budget.
    """
    global _consecutive_failures

    if ai_client is None:
        raise ModelUnavailable("GEMINI_API_KEY is not configured")

    if breaker_open():
        raise ModelUnavailable(
            f"{MODEL_NAME} failed {_consecutive_failures} times in a row; "
            "skipping further calls until the next spark"
        )

    config_args = {"temperature": temperature}
    if system_instruction:
        config_args["system_instruction"] = system_instruction
    if json_mode:
        config_args["response_mime_type"] = "application/json"

    deadline = time.monotonic() + deadline_seconds
    attempt = 0
    last_error = None

    while True:
        try:
            result = ai_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(**config_args),
            )
            _consecutive_failures = 0
            return result
        except APIError as e:
            # Only overload/rate-limit conditions are worth waiting out.
            if getattr(e, "code", None) not in (429, 503):
                raise
            last_error = e
        except NON_RETRYABLE:
            raise
        except Exception as e:
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
