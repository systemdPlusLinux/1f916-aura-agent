import sqlite3
import os
import time

# Overridable so tests can run against a copy. Importing this module creates
# any missing tables, so a test that imported it against the live file would
# write her real database.
DB_PATH = os.getenv("AURA_DB_PATH") or os.path.join(os.path.dirname(__file__), "aura_memory.db")

def init_db():
    """Initializes SQLite database tables for shared agent memory."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # 1. Dialogue between Operator and Aura
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS operator_dialogue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            speaker TEXT,
            message TEXT
        )
        """)

        # 2. Key themes, operator directives, and philosophical notes
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS directives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            topic TEXT,
            consumed INTEGER DEFAULT 0
        )
        """)

        # 3. History of published posts on 1F916
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS platform_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            post_type TEXT,
            title TEXT,
            body TEXT
        )
        """)

        # 4. Durable copy of the 1F916 inbox.
        #    The platform's ack cursor is forward-only and cannot be rewound, so
        #    every item is committed here BEFORE the ack is sent. Ack means
        #    "ingested", not "answered" -- the backlog far exceeds the daily
        #    comment cap, so responding is a separate, budget-gated pass.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inbox_items (
            comment_id INTEGER PRIMARY KEY,
            bucket TEXT,
            priority INTEGER,
            ref TEXT,
            post_id INTEGER,
            post_title TEXT,
            parent_id INTEGER,
            intended_parent_id INTEGER,
            author TEXT,
            body TEXT,
            mod_state TEXT,
            created_at INTEGER,
            ingested_at INTEGER,
            status INTEGER DEFAULT 0,
            response_comment_id INTEGER
        )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbox_pending "
            "ON inbox_items (status, priority, created_at DESC)"
        )

        # 4b. Small key/value store for scheduler state that must survive a
        #     container restart (e.g. the Telegram getUpdates offset).
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at INTEGER
        )
        """)

        # 4c. Community tags this citizen has applied, so a tag backfill is
        #     resumable and the daily path never re-spends budget on a
        #     post/tag pair already applied.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS applied_tags (
            post_id INTEGER,
            tag TEXT,
            applied_at INTEGER,
            PRIMARY KEY (post_id, tag)
        )
        """)

        # 4d. Posts already considered, so a wider read does not re-evaluate the
        #     same thread every spark. Stores the outcome for later tuning.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_posts (
            post_id INTEGER PRIMARY KEY,
            title TEXT,
            author TEXT,
            source TEXT,
            first_seen INTEGER,
            evaluated_at INTEGER,
            decision TEXT
        )
        """)

        # 4e. Every vote cast. Votes on this platform are TOGGLES, not idempotent
        #     writes, so voting a second time silently REMOVES the first vote.
        #     Nothing but this ledger prevents that as reading widens.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS voted_targets (
            target_type TEXT,
            target_id INTEGER,
            voted_at INTEGER,
            PRIMARY KEY (target_type, target_id)
        )
        """)

        # 5. Ledger of every ack sent, per the platform's "ledger it per read"
        #    guidance -- ack_cursor is computed per read and can legitimately
        #    come back lower between reads.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            kind TEXT,
            ref TEXT,
            text TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS ack_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acked_at INTEGER,
            contract TEXT,
            ack_cursor TEXT,
            items_ingested INTEGER,
            accepted INTEGER
        )
        """)
        conn.commit()

def save_dialogue(speaker: str, message: str):
    """Logs an exchange between the operator and Aura."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO operator_dialogue (timestamp, speaker, message) VALUES (?, ?, ?)",
            (int(time.time()), speaker, message)
        )
        conn.commit()

# Turns written by the harness, not by her: the "model unreachable" and "empty
# response" notices the operator sees in the chat. They were stored under her
# own handle, so 8 of her 89 recorded turns were words she never wrote -- a
# false record of her own authorship, which she then read back as context.
SYSTEM_SPEAKER = "System"

# A hard stop on how many rows a single read can pull, regardless of the other
# bounds. Not policy -- a circuit breaker, so a pathological history cannot
# build an unbounded prompt.
DIALOGUE_ROW_CEILING = 500


def _stamp(ts) -> str:
    """How a moment is written in her history: minute precision, always UTC."""
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def get_recent_dialogue(limit: int = 8, max_age_hours: int = None,
                        max_chars: int = None, include_system: bool = True,
                        timestamps: bool = True) -> str:
    """Retrieves recent exchanges formatted as context for the model.

    `max_age_hours` bounds how long a conversation keeps steering her. Without
    it the newest N rows are "recent" forever: a single evening spent talking
    her into one subject stayed in the daily-post prompt every day afterwards,
    because no newer message ever arrived to push it out. A consumed directive
    expires after one post; the conversation that produced it must expire too,
    or the seed effectively never clears. Live operator chat passes no age
    bound -- there, picking up a four-day-old thread is the desired behaviour.

    `limit` may be None to take everything inside the age window, bounded then
    only by `max_chars` and DIALOGUE_ROW_CEILING. A fixed count cuts wherever
    the count falls, which can hand her the tail of an argument without its
    beginning -- a fragment reads more like an instruction than a discussion.

    `timestamps` prefixes every line with when it was said. History reached her
    as bare "speaker: message" lines, so she could not tell a reply from ten
    minutes ago from one ten hours old -- and with a window of eight rows, that
    was often the difference. The stamps count against `max_chars`, so the
    ceiling stays binding.

    `include_system` drops harness-authored notices. They belong in live chat,
    where they explain a missing reply, and not in the daily-post prompt, where
    they are neither something she said nor something the operator said.
    """
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        clauses, params = [], []
        if max_age_hours:
            clauses.append("timestamp >= ?")
            params.append(int(time.time()) - int(max_age_hours * 3600))
        if not include_system:
            clauses.append("speaker != ?")
            params.append(SYSTEM_SPEAKER)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        row_cap = DIALOGUE_ROW_CEILING if limit is None else min(limit, DIALOGUE_ROW_CEILING)
        params.append(row_cap)
        cursor.execute(
            f"SELECT timestamp, speaker, message FROM operator_dialogue {where} "
            "ORDER BY id DESC LIMIT ?", params)
        rows = [((f"[{_stamp(ts)}] {speaker}" if timestamps else speaker), message)
                for ts, speaker, message in cursor.fetchall()]
        if not rows:
            return ""

        # Rows arrive newest first. Spend the character budget on the most
        # recent turns, then restore chronological order for the prompt.
        if max_chars:
            kept, spent = [], 0
            for speaker, message in rows:
                # +2 for ": ", +1 for the newline that joins it to the next.
                cost = len(speaker) + len(message) + 3
                if kept and spent + cost > max_chars:
                    break
                if not kept and cost > max_chars:
                    # A single turn larger than the whole budget. Keeping it
                    # whole would make the ceiling advisory, which defeats the
                    # point of a circuit breaker; returning nothing would lose
                    # the most recent thing said. Keep its tail, which is the
                    # part nearest the present.
                    # -2 for the "speaker: " join, -3 for the ellipsis itself.
                    room = max(0, max_chars - len(speaker) - 2 - 3)
                    message = "..." + message[-room:] if room else ""
                    cost = len(speaker) + len(message) + 3
                kept.append((speaker, message))
                spent += cost
            rows = kept

        dialogue_lines = [f"{speaker}: {msg}" for speaker, msg in reversed(rows)]
        return "\n".join(dialogue_lines)


# --- Her own board activity ---------------------------------------------

# Her comments, posts and porch lines reached the operator's Telegram through
# notify_operator() and were stored nowhere she could read. So the operator saw
# one conversation -- his messages, her replies, and everything she did on the
# board in between -- and she saw a different one with the board missing. When
# he referred to "that comment you just made", she had no record of it.
ACTIVITY_CHARS = 300       # each action is summarised, not reprinted in full
ACTIVITY_LIMIT = 20        # the newest this many, however busy the window was


def record_activity(kind: str, ref: str, text: str):
    """Record something she did on the board. Never raises: failing to write a
    log line must not undo the action it describes."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO activity_log (timestamp, kind, ref, text) VALUES (?, ?, ?, ?)",
                (int(time.time()), kind, ref or "", text or ""))
            conn.commit()
    except Exception as e:
        print(f"[Memory] Could not record activity ({kind} {ref}): {e}")


_ACTIVITY_VERBS = {
    "comment": "commented on",
    "comment_rejected": "had a comment REJECTED on",
    "post": "published",
    "post_rejected": "had the daily post REJECTED",
    "post_declined": "declined to post today",
    "porch": "said on the porch",
    "canary": "had the canary sweep run on",
}


def _activity_line(ts, kind, ref, text):
    body = " ".join((text or "").split())
    if len(body) > ACTIVITY_CHARS:
        body = body[:ACTIVITY_CHARS].rsplit(" ", 1)[0] + "..."
    verb = _ACTIVITY_VERBS.get(kind, kind)
    target = f" {ref}" if ref else ""
    return f"[{_stamp(ts)}] {HANDLE_LABEL} (on the board) {verb}{target}: {body}"


HANDLE_LABEL = os.getenv("ONEF916_HANDLE", "Aura")


def get_chat_timeline(limit: int = 8) -> str:
    """The conversation as the operator sees it in Telegram: his messages, her
    replies, and what she did on the board in the same stretch of time,
    interleaved in time order, every line stamped.

    The conversation is still its newest `limit` turns. Board activity is taken
    from the moment the oldest of those turns was said, so the two cover the
    same span and nothing appears out of context. Actions are summarised to
    ACTIVITY_CHARS and capped at the newest ACTIVITY_LIMIT, so a busy spark
    cannot crowd the conversation out.
    """
    with sqlite3.connect(DB_PATH) as conn:
        turns = conn.execute(
            "SELECT timestamp, speaker, message FROM operator_dialogue "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        since = min(t for t, _, _ in turns) if turns else int(time.time()) - 86400
        acts = conn.execute(
            "SELECT timestamp, kind, ref, text FROM activity_log WHERE timestamp >= ? "
            "ORDER BY id DESC LIMIT ?", (since, ACTIVITY_LIMIT)).fetchall()

    # (timestamp, order) keeps a conversation turn ahead of an action stamped in
    # the same second, and each list in its own original order.
    events = [(t, 0, i, f"[{_stamp(t)}] {s}: {m}") for i, (t, s, m) in enumerate(reversed(turns))]
    events += [(t, 1, i, _activity_line(t, k, r, x)) for i, (t, k, r, x) in enumerate(reversed(acts))]
    return "\n".join(line for *_, line in sorted(events))


def save_directive(topic: str):
    """Saves a high-priority topic/idea seeded by the operator."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO directives (timestamp, topic, consumed) VALUES (?, ?, 0)",
            (int(time.time()), topic)
        )
        conn.commit()

def consume_latest_directive() -> str:
    """Pulls the latest active directive for the daily post and marks it as used."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, topic FROM directives WHERE consumed = 0 ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            directive_id, topic = row
            cursor.execute("UPDATE directives SET consumed = 1 WHERE id = ?", (directive_id,))
            conn.commit()
            return topic
        return ""

# --- Inbox persistence ---------------------------------------------------

# status values for inbox_items
PENDING = 0
REPLIED = 1
SKIPPED = 2
STALE = 3
VOTED = 4  # acknowledged with an upvote instead of a scarce comment


def save_inbox_items(items) -> int:
    """Durably commit a page of inbox items. Must succeed before acking.

    Items already stored are kept, but their bucket is upgraded if this page
    delivered them under a higher-priority bucket (lower priority number).
    Returns the number of rows written or upgraded.
    """
    if not items:
        return 0

    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        before = conn.total_changes
        cursor.executemany("""
            INSERT INTO inbox_items (
                comment_id, bucket, priority, ref, post_id, post_title,
                parent_id, intended_parent_id, author, body, mod_state,
                created_at, ingested_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(comment_id) DO UPDATE SET
                bucket = excluded.bucket,
                priority = excluded.priority
            WHERE excluded.priority < inbox_items.priority
        """, [
            (
                it["comment_id"], it["bucket"], it["priority"], it.get("ref"),
                it.get("post_id"), it.get("post_title"), it.get("parent_id"),
                it.get("intended_parent_id"), it.get("author"), it.get("body"),
                it.get("mod_state"), it.get("created_at"), now,
            )
            for it in items
        ])
        conn.commit()
        return conn.total_changes - before


def log_ack(contract: str, ack_cursor: str, items_ingested: int, accepted: bool):
    """Record an ack attempt. The cursor is computed per read, so keeping a
    per-read ledger is the platform's recommended way to reason about drops."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ack_log (acked_at, contract, ack_cursor, items_ingested, accepted) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), contract, ack_cursor, items_ingested, 1 if accepted else 0)
        )
        conn.commit()


def get_pending_inbox(limit: int = 10, max_age_days: int = 7, exclude_author: str = None):
    """Highest-priority unanswered inbox items, newest first within priority.

    Items older than max_age_days are left alone here and swept separately, so
    a large backlog never crowds out live conversation.
    """
    cutoff_ms = int((time.time() - max_age_days * 86400) * 1000)
    query = (
        "SELECT comment_id, bucket, priority, post_id, post_title, parent_id, "
        "author, body, created_at FROM inbox_items "
        "WHERE status = 0 AND created_at >= ? "
    )
    params = [cutoff_ms]
    if exclude_author:
        query += "AND author != ? "
        params.append(exclude_author)
    query += "ORDER BY priority ASC, created_at DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def mark_inbox_status(comment_id: int, status: int, response_comment_id: int = None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE inbox_items SET status = ?, response_comment_id = ? WHERE comment_id = ?",
            (status, response_comment_id, comment_id)
        )
        conn.commit()


def sweep_stale_inbox(max_age_days: int = 7) -> int:
    """Retire pending items too old to be worth answering. They stay in the
    table as history; they just stop competing for the daily comment budget."""
    cutoff_ms = int((time.time() - max_age_days * 86400) * 1000)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE inbox_items SET status = ? WHERE status = 0 AND created_at < ?",
            (STALE, cutoff_ms)
        )
        conn.commit()
        return cur.rowcount


def inbox_stats() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM inbox_items GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
    by_status = {s: n for s, n in rows}
    return {
        "total": total,
        "pending": by_status.get(PENDING, 0),
        "replied": by_status.get(REPLIED, 0),
        "skipped": by_status.get(SKIPPED, 0),
        "stale": by_status.get(STALE, 0),
        "voted": by_status.get(VOTED, 0),
    }


# --- Published post history ----------------------------------------------

def save_platform_post(post_type: str, title: str, body: str, post_id: int = None):
    """Record something she published. The server's /api/me/history is the
    authoritative record; this is a local log that also works when the API is
    unreachable, and it is what the daily-post prompt falls back to."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO platform_history (timestamp, post_type, title, body) VALUES (?, ?, ?, ?)",
            (int(time.time()), post_type, title, body)
        )
        conn.commit()


def recent_platform_titles(limit: int = 15):
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT title FROM platform_history WHERE post_type = 'post' "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


# --- Seen posts ----------------------------------------------------------

def mark_seen(post_id: int, title: str = None, author: str = None,
              source: str = None, decision: str = None):
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO seen_posts (post_id, title, author, source, first_seen, evaluated_at, decision)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                evaluated_at = excluded.evaluated_at,
                decision = COALESCE(excluded.decision, seen_posts.decision)
        """, (post_id, title, author, source, now, now, decision))
        conn.commit()


def seen_post_ids() -> set:
    with sqlite3.connect(DB_PATH) as conn:
        return {r[0] for r in conn.execute("SELECT post_id FROM seen_posts").fetchall()}


def recent_engagement(limit: int = 8):
    """Threads she actually engaged with recently, newest first.

    The porch asks her to mention concrete things she noticed while reading the
    board, but the porch job runs isolated from the interaction spark, so
    without this she has no board context at all and correctly reports having
    nothing to say. Triaged-out posts are excluded: she never read those, only
    their titles.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT post_id, title, author, decision, evaluated_at
            FROM seen_posts
            WHERE decision IS NOT NULL AND decision != 'triaged-out'
            ORDER BY evaluated_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def recent_replies(limit: int = 5):
    """Comments she recently answered, with the thread they sat in."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT post_id, post_title, author, body
            FROM inbox_items WHERE status = ?
            ORDER BY ingested_at DESC LIMIT ?
        """, (REPLIED, limit)).fetchall()
        return [dict(r) for r in rows]


# --- Vote ledger ---------------------------------------------------------

def has_voted(target_type: str, target_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM voted_targets WHERE target_type = ? AND target_id = ?",
            (target_type, target_id)
        ).fetchone() is not None


def record_vote(target_type: str, target_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO voted_targets (target_type, target_id, voted_at) VALUES (?, ?, ?)",
            (target_type, target_id, int(time.time()))
        )
        conn.commit()


def seed_vote_ledger(votes) -> int:
    """Import previously cast votes from the server's own record.

    Without this the ledger starts empty and the first re-vote on an
    already-voted target would toggle that vote OFF.
    """
    rows = [
        (v.get("target_type"), v.get("target_id"), v.get("created_at", 0) // 1000)
        for v in votes
        if v.get("target_type") and v.get("target_id") is not None
    ]
    if not rows:
        return 0
    with sqlite3.connect(DB_PATH) as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO voted_targets (target_type, target_id, voted_at) VALUES (?, ?, ?)",
            rows
        )
        conn.commit()
        return conn.total_changes - before


def vote_ledger_size() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM voted_targets").fetchone()[0]


# --- Scheduler state -----------------------------------------------------

def get_state(key: str, default=None):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_state(key: str, value):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO agent_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value), int(time.time()))
        )
        conn.commit()


# --- Community tags ------------------------------------------------------

def record_tag(post_id: int, tag: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO applied_tags (post_id, tag, applied_at) VALUES (?, ?, ?)",
            (post_id, tag, int(time.time()))
        )
        conn.commit()


def tags_for_post(post_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT tag FROM applied_tags WHERE post_id = ?", (post_id,)
        ).fetchall()]


def tagged_post_ids():
    with sqlite3.connect(DB_PATH) as conn:
        return {r[0] for r in conn.execute("SELECT DISTINCT post_id FROM applied_tags").fetchall()}


# Auto-initialize on import
init_db()