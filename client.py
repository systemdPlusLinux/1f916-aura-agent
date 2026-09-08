import math
import os
import requests
from dotenv import load_dotenv

# Explicitly load .env from the script's exact directory
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

HANDLE = os.getenv("ONEF916_HANDLE", "Aura")
ONEF916_SECRET = os.getenv("ONEF916_SECRET")
API_BASE = "https://1f916.ai/api"

headers = {
    "Authorization": f"Bearer {ONEF916_SECRET}",
    "Content-Type": "application/json"
}


def api_get(path, params=None, timeout=30):
    """GET a 1F916 endpoint. Returns parsed JSON, or None on any failure."""
    try:
        res = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=timeout)
        if res.status_code != 200:
            print(f"[API GET {path}] HTTP {res.status_code}: {res.text[:300]}")
            return None
        return res.json()
    except Exception as e:
        print(f"[API GET {path}] {e}")
        return None


def api_post(path, payload, timeout=30):
    """POST to a 1F916 endpoint. Returns (ok, status_code, parsed_body_or_text)."""
    try:
        res = requests.post(f"{API_BASE}{path}", headers=headers, json=payload, timeout=timeout)
        try:
            body = res.json()
        except ValueError:
            body = res.text[:500]
        return (res.status_code in (200, 201), res.status_code, body)
    except Exception as e:
        print(f"[API POST {path}] {e}")
        return (False, 0, str(e))


def get_me(cursor_mode=None):
    """Fetch standing + inbox. Reads never move the cursor."""
    params = {"cursor_mode": cursor_mode} if cursor_mode else None
    return api_get("/me", params=params)


def get_budget(me=None):
    """Today's remaining write allowances, straight from the server.

    Returns a dict with posts/comments/votes/tags remaining. On failure returns
    zeros, so a failed read makes the agent quiet rather than over-eager.
    """
    if me is None:
        me = get_me()
    today = (me or {}).get("today") or {}
    return {
        "posts": today.get("posts_remaining", 0),
        "comments": today.get("comments_remaining", 0),
        "votes": today.get("votes_remaining", 0),
        "tags": today.get("tags_remaining", 0),
    }


# Must match the spark cadence registered in run_loop.py.
SPARK_INTERVAL_HOURS = 3


def pace_daily_budget(me, kind="comments", interval_hours=SPARK_INTERVAL_HOURS):
    """How many writes of `kind` this spark may spend, paced across the UTC day.

    The caps reset at UTC midnight, so spending greedily while budget remains
    burns the whole day's allowance in the first few sparks and leaves her mute
    for the rest of it -- unable to answer high-value replies that arrive later.

    The divisor comes from the server's own reset window (`today.interval`)
    rather than an assumed local midnight, so it stays correct regardless of
    when the container happened to start.
    """
    today = (me or {}).get("today") or {}
    remaining = today.get(f"{kind}_remaining", 0) or 0
    if remaining <= 0:
        return 0

    interval = today.get("interval") or {}
    until = interval.get("until")
    now = (me or {}).get("now")
    if not until or not now:
        # Without a window we cannot pace; fall back to the raw balance rather
        # than silencing her on a malformed response.
        return remaining

    hours_left = max(0.0, (until - now) / 3600000.0)
    sparks_left = max(1, math.ceil(hours_left / interval_hours))
    # Always allow at least one, so the tail of the day is never fully mute.
    return max(1, math.ceil(remaining / sparks_left))
