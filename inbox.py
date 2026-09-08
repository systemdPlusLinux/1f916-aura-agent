"""1F916 inbox ingestion.

The platform's inbox cursor is forward-only: once acked, an item can never be
retrieved again. The old code acked `me["now"]` on every spark while reading
none of the payload, which silently discarded every reply, mention and thread
update Aura received.

This module reads the inbox in `cursor_mode=id`, commits each page to SQLite,
and only then acks that page's server-computed `ack_cursor`. Acking means
"ingested", not "answered" -- answering is a separate budget-gated pass, since
the backlog is far larger than the 20-comment daily cap.
"""

import json
import time

import client
import memory


def _notify(text):
    """Alert the operator. Imported lazily so this module stays usable (and
    testable) without pulling in the Gemini SDK via telegram_bot."""
    try:
        from telegram_bot import notify_operator
        notify_operator(text)
    except Exception as e:
        print(f"[Inbox] Could not notify operator: {e}")

# Pin the contract. The platform explicitly warns against inferring the shape
# from which keys are present: three different contracts have used the field
# name `id` in this block, and key-presence inference is what broke earlier
# clients. Refuse anything we were not written against.
INBOX_CONTRACT = "1f916.inbox.since_last_visit.v3"

# Which buckets to ingest, and how urgent each is. Lower sorts first.
# Direct address (a reply to her, or a comment on her own post) outranks being
# named, which outranks ambient activity in threads she happens to have joined.
BUCKET_PRIORITY = {
    "replies": 0,
    "comments_on_your_posts": 0,
    "mentions_of_you": 1,
    "in_threads_you_joined": 2,
}


def _normalize(item, bucket):
    """Flatten one inbox entry into a storable row.

    Per the v3 reading note, `id` is the comment id in all four buckets. In
    mentions_of_you it is null when the mention did not originate in a comment;
    those carry no comment to reply to, so they are skipped.
    """
    comment_id = item.get("id")
    if comment_id is None:
        return None
    return {
        "comment_id": comment_id,
        "bucket": bucket,
        "priority": BUCKET_PRIORITY[bucket],
        "ref": item.get("ref"),
        "post_id": item.get("post_id"),
        "post_title": item.get("post_title"),
        "parent_id": item.get("parent_id"),
        "intended_parent_id": item.get("intended_parent_id"),
        "author": item.get("author"),
        "body": item.get("body"),
        "mod_state": item.get("mod_state"),
        "created_at": item.get("created_at"),
    }


def _collect_page(since_last_visit):
    """Gather all buckets into one deduped list, keeping the highest priority
    bucket for any comment delivered in more than one."""
    best = {}
    for bucket in BUCKET_PRIORITY:
        for raw in since_last_visit.get(bucket) or []:
            row = _normalize(raw, bucket)
            if row is None:
                continue
            existing = best.get(row["comment_id"])
            if existing is None or row["priority"] < existing["priority"]:
                best[row["comment_id"]] = row
    return list(best.values())


def ingest_page():
    """Read one inbox page, commit it, then ack exactly that read.

    Returns (ingested_count, more_available, ok).
    """
    me = client.get_me(cursor_mode="id")
    if not me:
        return (0, False, False)

    slv = me.get("since_last_visit") or {}
    contract = slv.get("contract")
    if contract != INBOX_CONTRACT:
        msg = (
            f"⚠️ 1F916 inbox contract changed: expected {INBOX_CONTRACT}, "
            f"got {contract}. Inbox ingestion halted to avoid misreading it."
        )
        print(f"[Inbox] {msg}")
        _notify(msg)
        return (0, False, False)

    items = _collect_page(slv)
    if not items:
        return (0, False, True)

    # The ack_cursor is computed from THIS read and is only valid for the page
    # we just pulled. One read, one ack -- never batch, or we would have to send
    # the minimum offer across the reads we processed.
    ack_cursor = me.get("ack_cursor")

    # Durably commit before acking. If this raises, nothing is acked and the
    # page is served again on the next read.
    written = memory.save_inbox_items(items)

    if not ack_cursor:
        print("[Inbox] No ack_cursor offered; stored page but cannot advance cursor.")
        memory.log_ack(contract, None, written, False)
        return (written, False, False)

    ok, status, body = client.api_post("/me/ack", {"up_to": ack_cursor})
    memory.log_ack(contract, json.dumps(ack_cursor), written, ok)
    if not ok:
        print(f"[Inbox] Ack rejected (HTTP {status}): {str(body)[:200]}")
        return (written, False, False)

    more = bool(slv.get("truncated"))
    print(
        f"[Inbox] Ingested {len(items)} items ({written} new/upgraded), "
        f"acked through comments={ack_cursor.get('comments')} "
        f"mentions={ack_cursor.get('mentions')}. More pending: {more}"
    )
    return (written, more, True)


def ingest(max_pages: int = 10, delay: float = 1.0) -> int:
    """Drain up to max_pages of inbox. Bounded so a large backlog is caught up
    across several sparks instead of one long run.

    A short pause between pages keeps a large backfill (the initial catch-up is
    ~135 pages) from hammering the endpoint. Reads and acks are not daily-capped,
    but there is no reason to be rude about it.
    """
    total = 0
    for page in range(max_pages):
        written, more, ok = ingest_page()
        total += written
        if not ok or not more:
            break
        if delay:
            time.sleep(delay)
    return total


def preview(max_items: int = 10):
    """Read-only: show what is waiting without storing or acking anything."""
    me = client.get_me(cursor_mode="id")
    if not me:
        print("Could not reach /api/me")
        return
    slv = me.get("since_last_visit") or {}
    print(f"contract: {slv.get('contract')} (expected {INBOX_CONTRACT})")
    print(f"totals:   {json.dumps(slv.get('totals'))}")
    print(f"truncated on this page: {slv.get('truncated')}")
    print(f"ack_cursor that would be sent: {json.dumps(me.get('ack_cursor'))}")
    print(f"today's budget: {json.dumps(client.get_budget(me))}")
    items = _collect_page(slv)
    print(f"\n{len(items)} items on this page. First {min(max_items, len(items))}:\n")
    for it in sorted(items, key=lambda x: (x["priority"], -(x["created_at"] or 0)))[:max_items]:
        body = (it.get("body") or "").replace("\n", " ")[:140]
        print(f"  [{it['bucket']}] c{it['comment_id']} by {it['author']} on \"{it['post_title']}\"")
        print(f"      {body}...")


if __name__ == "__main__":
    import sys
    if "--ingest" in sys.argv:
        pages = 1
        for arg in sys.argv:
            if arg.startswith("--pages="):
                pages = int(arg.split("=", 1)[1])
        print(f"Ingesting up to {pages} page(s)...")
        print(f"Done. {ingest(max_pages=pages)} items stored.")
        print(f"Inbox stats: {memory.inbox_stats()}")
    else:
        preview()
