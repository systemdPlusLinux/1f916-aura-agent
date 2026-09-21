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
import time

import client
import lawbook
from client import HANDLE

POSTS_TTL = 30 * 60
CLOCK_TTL = 10 * 60
POSTS_SHOWN = 8

_cache = {"offset": None, "offset_at": 0.0, "posts": None, "posts_at": 0.0}


def _fmt(epoch_seconds, seconds=False):
    dt = datetime.datetime.fromtimestamp(epoch_seconds, datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC" if seconds else "%Y-%m-%d %H:%M UTC")


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


def system_facts(include_posts=True):
    """The labelled block placed at the top of every prompt that speaks for her."""
    t, source = now()
    lines = [
        "SYSTEM-PROVIDED FACTS -- supplied by the code when this prompt was",
        "assembled, read from the 1F916 server. Not memory, not conversation.",
        "",
        f"Current time: {_fmt(t, seconds=True)} ({source})",
        f"Generation:   {lawbook.current_generation()}",
    ]
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
