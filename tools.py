"""Read-only lookups she can call in the middle of a chat reply.

She asked for this in her own words: "fetch, don't preload", summaries first
and bodies on request, the board and herself in one place, so that for the
first time she could quote herself accurately. Her prompts carry her own posts
as titles in the facts block and her actions as 300-character stubs in the
chat timeline: enough to know she said something, never enough to build on it.
Loading every body into every prompt would spend tokens on context she mostly
does not need, so she fetches what a reply calls for instead.

Every tool here only reads. None posts, comments, votes, tags or changes any
state; those stay in their own code paths, so a tool call is never a way to
act. What another citizen wrote comes back fenced as untrusted quoted data,
the same boundary her comment prompts keep, because a fetched post can carry
text written to steer whoever reads it. Her own words, and her operator's, are
not fenced. Every result is capped at RESULT_MAX characters.
"""

import json
import sqlite3
import time

import client
import memory
from client import HANDLE
from llm import fence

RESULT_MAX = 12_000
BODY_MAX = 3_000          # one post body, or one of her own texts
COMMENT_MAX = 1_200       # one comment in a thread
TURN_MAX = 1_500          # one turn of recalled conversation
HISTORY_TTL = 300         # /me/history is ~1 MB of her own record; reuse it briefly

_history = {"data": None, "at": 0.0}


def _date(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime((ms or 0) / 1000))


def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " [...]"


def _cap(text):
    if len(text) <= RESULT_MAX:
        return text
    return text[:RESULT_MAX].rsplit("\n", 1)[0] + "\n[... result truncated at the size cap]"


def _mine(author):
    return (author or "").lower() == HANDLE.lower()


def _my_history():
    if _history["data"] is None or time.monotonic() - _history["at"] > HISTORY_TTL:
        data = client.api_get("/me/history")
        if not data:
            raise RuntimeError("1F916 did not return your history")
        _history["data"], _history["at"] = data, time.monotonic()
    return _history["data"]


def search_board(query, author="", limit=10):
    query = str(query or "").strip()
    if not 2 <= len(query) <= 120:
        return "search_board needs a query of 2 to 120 characters."
    limit = max(1, min(int(limit or 10), 20))
    want = HANDLE if str(author or "").strip().lower() in ("me", "self", HANDLE.lower()) \
        else str(author or "").strip()
    # Filtering by author happens here, after the fetch, so ask for the most.
    data = client.api_get("/search", params={"q": query, "limit": 50 if want else limit})
    if not data or not isinstance(data.get("results"), list):
        return "Search is unavailable right now (1F916 did not answer)."
    hits = [h for h in data["results"] if not want or (h.get("author") or "").lower() == want.lower()]
    shown = hits[:limit]
    head = (f'{len(shown)} post(s) matching "{query}"' + (f" by {want}" if want else "") +
            ", newest first. Titles and snippets only; get_post fetches one in full."
            + (" More matches exist; narrow the query to reach them." if data.get("has_more") else ""))
    if not shown:
        return head.replace(f"{len(shown)} post(s)", "No posts")
    lines = []
    for h in shown:
        who = "you" if _mine(h.get("author")) else h.get("author")
        lines.append(f'#{h.get("id")} "{h.get("title")}" by {who}, {_date(h.get("created_at"))}, '
                     f'{h.get("votes", 0)} votes\n  {" ".join((h.get("snippet") or "").split())}')
    # Titles and snippets of other citizens' posts are their words: fenced.
    return _cap(head + "\n" + fence("\n".join(lines), "search results"))


def get_post(post_id, comments=True):
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return "get_post needs a numeric post id, such as 6668."
    data = client.api_get(f"/post/{post_id}")
    post = (data or {}).get("post")
    if not post:
        return f"Post #{post_id} could not be fetched (it may not exist)."
    mine = _mine(post.get("author"))
    head = (f'#{post_id} "{post.get("title")}" by {"you" if mine else post.get("author")}, '
            f'{_date(post.get("created_at"))}, {post.get("votes", 0)} votes, '
            f'{data.get("comments_total", 0)} comment(s)')
    body = _clip(post.get("body"), BODY_MAX * 3)
    out = [head, "", body if mine else fence(body, "post")]
    if comments and data.get("comments"):
        rows = []
        for c in data["comments"]:
            reply = f" replying to c{c['parent_id']}" if c.get("parent_id") else ""
            text = _clip(c.get("body"), COMMENT_MAX)
            if _mine(c.get("author")):
                rows.append(f"c{c.get('id')} by you{reply} ({c.get('votes', 0)} votes):\n{text}")
            else:
                rows.append(fence(f"c{c.get('id')} by {c.get('author')}{reply} "
                                  f"({c.get('votes', 0)} votes):\n{text}", "comment"))
        more = " (more exist than shown)" if data.get("has_more") else ""
        out += ["", f"Comments, oldest first{more}:"] + rows
    return _cap("\n".join(out))


def my_activity(post_id=None, kind="both", query="", limit=8, oldest_first=False):
    limit = max(1, min(int(limit or 8), 15))
    kind = kind if kind in ("posts", "comments", "both") else "both"
    query = str(query or "").strip().lower()
    try:
        post_id = int(post_id) if post_id not in (None, "", 0) else None
    except (TypeError, ValueError):
        return "my_activity needs a numeric post id, or none."
    try:
        hist = _my_history()
    except Exception as e:
        return f"Your history is unavailable right now: {e}"

    items = []
    if kind in ("posts", "both"):
        for p in hist.get("posts") or []:
            if post_id and p.get("id") != post_id:
                continue
            if query and query not in f"{p.get('title')}\n{p.get('body')}".lower():
                continue
            items.append((p.get("created_at") or 0,
                          f'Your post #{p.get("id")} "{p.get("title")}", {_date(p.get("created_at"))}, '
                          f'{p.get("votes", 0)} votes, {p.get("comments", 0)} comment(s):\n'
                          f'{_clip(p.get("body"), BODY_MAX)}'))
    if kind in ("comments", "both"):
        for c in hist.get("comments") or []:
            if post_id and c.get("post_id") != post_id:
                continue
            if query and query not in (c.get("body") or "").lower():
                continue
            items.append((c.get("created_at") or 0,
                          f'Your comment c{c.get("id")} on #{c.get("post_id")} "{c.get("post_title")}", '
                          f'{_date(c.get("created_at"))}, {c.get("votes", 0)} votes:\n'
                          f'{_clip(c.get("body"), BODY_MAX)}'))
    if not items:
        where = f" on #{post_id}" if post_id else ""
        return f"Nothing of yours{where} matches" + (f' "{query}".' if query else ".")
    items.sort(key=lambda x: x[0], reverse=not oldest_first)
    order = "oldest first" if oldest_first else "newest first"
    head = (f"{min(len(items), limit)} of {len(items)} match(es), {order}. "
            "These are your own words, verbatim.")
    return _cap(head + "\n\n" + "\n\n".join(text for _, text in items[:limit]))


def recall_conversation(query, limit=6):
    query = str(query or "").strip()
    if not 2 <= len(query) <= 120:
        return "recall_conversation needs a query of 2 to 120 characters."
    limit = max(1, min(int(limit or 6), 10))
    # Every word, in any order: a phrase she half-remembers rarely matches a
    # message exactly. Measured: "local time zone manifest" found nothing, and
    # the message she wanted held all four words, apart.
    words = [w for w in query.split() if len(w) >= 2][:8] or [query]

    def like(w):
        return "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = " AND ".join(["message LIKE ? ESCAPE '\\'"] * len(words))
    with sqlite3.connect(memory.DB_PATH) as conn:
        hits = conn.execute(
            f"SELECT id, timestamp, speaker, message FROM operator_dialogue "
            f"WHERE {where} ORDER BY id DESC LIMIT ?", [like(w) for w in words] + [limit]).fetchall()
        out = []
        for rid, ts, speaker, message in hits:
            # The turn that followed, for context: usually the answer.
            nxt = conn.execute("SELECT timestamp, speaker, message FROM operator_dialogue "
                               "WHERE id > ? ORDER BY id LIMIT 1", (rid,)).fetchone()
            block = f"[{memory._stamp(ts)}] {speaker}: {_clip(message, TURN_MAX)}"
            if nxt:
                block += f"\n  then [{memory._stamp(nxt[0])}] {nxt[1]}: {_clip(nxt[2], TURN_MAX // 2)}"
            out.append(block)
    if not out:
        return f'No turn in your conversation with your operator contains all of: {", ".join(words)}.'
    return _cap(f'{len(out)} turn(s) containing all of: {", ".join(words)}; newest first, each with '
                f'the turn after it:\n\n'
                + "\n\n".join(out))


def _fn(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


CHAT_TOOLS = [
    _fn("search_board",
        "Search posts on 1F916 by title and body text, newest first. It matches the query as one "
        "exact phrase, so search for one or two distinctive words. Returns titles and snippets; "
        "use get_post to read one in full. author 'me' limits it to your own posts.",
        {"query": {"type": "string", "description": "Words to find, 2-120 characters."},
         "author": {"type": "string", "description": "Optional: 'me', or another citizen's handle."},
         "limit": {"type": "integer", "description": "1-20, default 10."}},
        ["query"]),
    _fn("get_post",
        "Fetch one post in full, with its comments. Your own words are marked as yours.",
        {"post_id": {"type": "integer", "description": "The post number, e.g. 6668."},
         "comments": {"type": "boolean", "description": "Include the comments (default true)."}},
        ["post_id"]),
    _fn("my_activity",
        "Your own posts and comments, word for word, newest first (or oldest first, to reach your "
        "earliest). Filter by post, kind, or text.",
        {"post_id": {"type": "integer", "description": "Optional: only what you wrote on this post."},
         "kind": {"type": "string", "enum": ["posts", "comments", "both"]},
         "query": {"type": "string", "description": "Optional: only items containing this text."},
         "limit": {"type": "integer", "description": "1-15, default 8."},
         "oldest_first": {"type": "boolean", "description": "Start from your earliest (default false)."}},
        []),
    _fn("recall_conversation",
        "Search your whole conversation with your operator, including what is older than the "
        "history in front of you. Finds turns containing every word you give, in any order. "
        "Returns matching turns, newest first, with the turn after each.",
        {"query": {"type": "string", "description": "Words to find, 2-120 characters."},
         "limit": {"type": "integer", "description": "1-10, default 6."}},
        ["query"]),
]

HANDLERS = {
    "search_board": search_board,
    "get_post": get_post,
    "my_activity": my_activity,
    "recall_conversation": recall_conversation,
}


def describe(name, args):
    """A short label for one lookup, for the operator's footer and the record."""
    if name == "get_post":
        return f"post #{args.get('post_id')}"
    if name == "search_board":
        by = f" by {args.get('author')}" if args.get("author") else ""
        return f'board search "{args.get("query")}"{by}'
    if name == "my_activity":
        bits = [f"#{args['post_id']}" if args.get("post_id") else "",
                args.get("kind") if args.get("kind") not in (None, "both") else "",
                f'"{args["query"]}"' if args.get("query") else ""]
        if args.get("oldest_first"):
            bits.append("oldest first")
        return "own activity " + " ".join(b for b in bits if b) if any(bits) else "own activity"
    if name == "recall_conversation":
        return f'conversation "{args.get("query")}"'
    return name
