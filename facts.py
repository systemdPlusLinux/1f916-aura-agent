"""System-provided facts: the time, and what she has recently published.

Nothing in any prompt used to carry the current date or time. She was left to
infer "when" from grammar -- on 2026-09-21 she concluded a post had "already
fired" from sentence tense, correctly, while being wrong about which version of
the system had fired it. The only dates she ever saw were static strings inside
lawbook entries. And only the daily post saw her own recent posts; chat, the
porch and her comment replies saw none, so she could not tell what she had
already said in public.

This module supplies both, from the 1F916 server rather than from memory, and
labels them as system-provided so they are never mistaken for conversation.

Two endpoints, chosen by weight. The public citizen record carries every post
with its timestamp and moderation state, but at 669 KB with its 500 comments it
is far too heavy to fetch per message; `comments_before=1` drops the comments
and brings it to ~97 KB. It is cached for 30 minutes and invalidated the moment
she publishes. The clock comes from /api/pulse, about 1 KB. What is cached is
the offset between the container clock and the server's, so every prompt gets
server time without a network call, re-checked at most every 10 minutes.

A failed read never fails a prompt. It falls back to the container clock and
says so in the label; the post list says it is unavailable rather than going
silently missing.
"""

import datetime
import os
import sys
import time

import client
import lawbook
from client import HANDLE

POSTS_TTL = 30 * 60
CLOCK_TTL = 10 * 60
POSTS_SHOWN = 8

# Her operator's wall clock. Given only UTC, she kept reasoning about the operator's
# day as if it ran on UTC. Phoenix has kept UTC-7 all year since 1968, so a fixed
# offset is exact and does not depend on the slim image carrying tz data.
OPERATOR_TZ_NAME = "America/Phoenix"
OPERATOR_TZ = datetime.timezone(datetime.timedelta(hours=-7), "MST")

_REPO = os.path.dirname(os.path.abspath(__file__))


def _git_head():
    """The commit the repository's HEAD points at, read from .git directly --
    the image has no git binary. None if it cannot be read."""
    try:
        with open(os.path.join(_REPO, ".git", "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        loose = os.path.join(_REPO, ".git", ref)
        if os.path.exists(loose):
            with open(loose) as f:
                return f.read().strip()
        with open(os.path.join(_REPO, ".git", "packed-refs")) as f:
            for line in f:
                if line.strip().endswith(" " + ref):
                    return line.split()[0]
    except OSError:
        pass
    return None


# Captured once, at import -- which is container start. The code she runs is
# whatever was loaded then, so this, not the repository's current HEAD, is her
# version. The two are compared on every read, so a commit that is on disk but
# not yet running is reported as exactly that instead of being silently assumed
# live: the version skew she named in id=184.
LOADED_COMMIT = _git_head()
LOADED_AT = time.time()

_cache = {"offset": None, "offset_at": 0.0, "posts": None, "posts_at": 0.0}


def _fmt(epoch_seconds, seconds=False):
    dt = datetime.datetime.fromtimestamp(epoch_seconds, datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC" if seconds else "%Y-%m-%d %H:%M UTC")


def _fmt_operator(epoch_seconds):
    dt = datetime.datetime.fromtimestamp(epoch_seconds, OPERATOR_TZ)
    return dt.strftime("%Y-%m-%d %H:%M MST (%a %-I:%M %p)")


def _learn_offset(payload):
    """Every 1F916 response carries `now` in epoch milliseconds."""
    now_ms = (payload or {}).get("now")
    if isinstance(now_ms, (int, float)):
        _cache["offset"] = now_ms / 1000.0 - time.time()
        _cache["offset_at"] = time.monotonic()


def _refresh_clock():
    if _cache["offset"] is not None and time.monotonic() - _cache["offset_at"] < CLOCK_TTL:
        return
    _learn_offset(client.api_get("/pulse", timeout=10))


def _refresh_posts():
    if _cache["posts"] is not None and time.monotonic() - _cache["posts_at"] < POSTS_TTL:
        return
    # comments_before=1 is an exclusive row id below every real comment, so it
    # returns posts only: ~97 KB instead of 669 KB.
    data = client.api_get(f"/citizen/{HANDLE}", params={"comments_before": 1}, timeout=15)
    if data and isinstance(data.get("posts"), list):
        _learn_offset(data)
        _cache["posts"] = sorted(data["posts"], key=lambda p: p.get("created_at") or 0,
                                 reverse=True)
        _cache["posts_at"] = time.monotonic()


def invalidate_posts():
    """Call after she publishes, so the next prompt sees the new post at once."""
    _cache["posts_at"] = 0.0


def now():
    """(epoch_seconds, source_label). Server time if ever reached, else local."""
    try:
        _refresh_clock()
    except Exception as e:
        print(f"[Facts] Clock refresh failed: {e}")
    if _cache["offset"] is None:
        return time.time(), "container clock -- 1F916 server unreachable"
    return time.time() + _cache["offset"], "1F916 server clock"


def post_status(post):
    state = post.get("mod_state")
    return "published" if not state else str(state)


def recent_posts(limit=POSTS_SHOWN):
    """Her newest posts as the server reports them, or None if unreachable."""
    try:
        _refresh_posts()
    except Exception as e:
        print(f"[Facts] Post refresh failed: {e}")
    return None if _cache["posts"] is None else _cache["posts"][:limit]


def status_by_id():
    """{post_id: status} for whatever is cached, for annotating other lists."""
    posts = recent_posts(limit=None) or []
    return {p.get("id"): post_status(p) for p in posts}


def _settings_lines():
    """Her configuration as the running modules hold it right now.

    Read from the loaded modules rather than copied here, so the numbers can
    never disagree with what is in effect. Uses sys.modules instead of imports:
    telegram_bot and spark_agent import this module, and anything not loaded
    (a standalone run) is simply left out rather than guessed.
    """
    lines = []
    head = _git_head()
    build = f"commit {LOADED_COMMIT[:7]}" if LOADED_COMMIT else "commit unknown"
    lines.append(f"Running build: {build}, loaded {_fmt(LOADED_AT)}")
    if head and LOADED_COMMIT and head != LOADED_COMMIT:
        lines.append(f"  Commit {head[:7]} is on disk but NOT running until the next restart.")

    m = sys.modules
    llm, tb, sa = m.get("llm"), m.get("telegram_bot"), m.get("spark_agent")
    if llm:
        chat = f", chat {tb.CHAT_REASONING_TOKENS:,}" if tb else ""
        lines.append(f"Model:         {llm.MODEL_NAME}, reasoning budget {llm.REASONING_MAX_TOKENS:,}{chat}")
    if tb:
        lines.append(f"Chat memory:   the newest {tb.CHAT_TURNS} conversation turns, plus your board "
                     "actions from the same span")
    if sa:
        lines.append(f"Post memory:   {sa.DIALOGUE_STEER_HOURS}h of conversation, capped at "
                     f"{sa.DIALOGUE_STEER_MAX_CHARS:,} chars; daily post from {sa.DAILY_POST_EARLIEST} UTC")
    return lines


def system_facts(include_posts=True):
    """The labelled block placed at the top of every prompt that speaks for her."""
    t, source = now()
    lines = [
        "SYSTEM-PROVIDED FACTS -- supplied by the code when this prompt was",
        "assembled, read from the 1F916 server. Not memory, not conversation.",
        "",
        f"Current time: {_fmt(t, seconds=True)} ({source})",
        f"Operator time: {_fmt_operator(t)} -- your operator's clock, {OPERATOR_TZ_NAME},",
        "               UTC-7 all year (no daylight saving). Every other stamp stays UTC.",
        f"Generation:   {lawbook.current_generation()}",
    ] + _settings_lines()
    if include_posts:
        posts = recent_posts()
        if posts is None:
            lines.append("Your recent posts: unavailable (1F916 unreachable).")
        elif not posts:
            lines.append("Your recent posts: none.")
        else:
            as_of = _cache["posts_at"]
            age = int((time.monotonic() - as_of) // 60) if as_of else 0
            lines.append(f"Your last {len(posts)} posts, newest first "
                         f"(votes and comments as of {age} min ago):")
            for p in posts:
                created = _fmt((p.get("created_at") or 0) / 1000.0)
                lines.append(
                    f"  #{p.get('id')}  {created}  {post_status(p):9}  "
                    f"{p.get('votes', 0)} votes, {p.get('comments', 0)} comments  "
                    f"\"{p.get('title')}\""
                )
    return "\n".join(lines)


if __name__ == "__main__":
    print(system_facts())
