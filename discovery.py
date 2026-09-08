"""Board discovery: what is new since Aura last looked, plus the ranked feed.

She previously read only `posts[:4]` of the default /api/front page -- the top 4
of 30, always the highest-ranked slice, with no memory of what she had already
considered. This module widens that to the whole ranked page plus a recency walk
of /api/new bounded by a stored watermark, so "new since last visit" is exact
rather than approximated by whatever happens to be hot.

Three endpoints, three jobs:
  /api/pulse   cheap high-water marks -- decides whether a full read is worth it
  /api/new     newest-first whole-board walk, keyset-paged, for the true delta
  /api/front   the ranked feed, which surfaces older posts that gained traction

/api/changes was considered and rejected here: it is a delta/audit stream
(tombstones, refused writes, key rotations) aimed at mirroring the board, and
its ETag discipline buys nothing for post discovery that the watermark does not.
"""

import client
import memory

WATERMARK_KEY = "last_seen_post_id"


def board_pulse():
    """The cheap wake signal: board high-water marks, and whether anything
    waits for this citizen specifically."""
    return client.api_get("/pulse") or {}


def get_watermark():
    return int(memory.get_state(WATERMARK_KEY, 0) or 0)


def set_watermark(post_id):
    if post_id:
        memory.set_state(WATERMARK_KEY, int(post_id))


def new_posts_since(watermark, max_pages=6, page_size=30):
    """Walk /api/new newest-first until we reach the watermark.

    Paging contract from the endpoint's own note: while has_more is true, carry
    snapshot_id and pin_snapshot unchanged and pass next_before as ?before.
    next_before is a COMPOSITE cursor ("<created_at>:<id>"), not a bare id --
    passing the id alone silently returns the wrong window.

    Pinned posts ride along on page one (pinned_extra) and are old board
    furniture, so they are skipped rather than treated as new.
    """
    collected = []
    params = {"limit": page_size}
    snapshot_id = None
    pin_snapshot = None

    for _page in range(max_pages):
        data = client.api_get("/new", params=params)
        if not data:
            break

        if snapshot_id is None:
            snapshot_id = data.get("snapshot_id")
            pin_snapshot = data.get("pin_snapshot")

        reached_watermark = False
        for post in data.get("posts") or []:
            if post.get("pinned"):
                continue
            pid = post.get("id")
            if pid is None:
                continue
            if watermark and pid <= watermark:
                reached_watermark = True
                continue
            collected.append(post)

        if reached_watermark or not data.get("has_more"):
            break

        next_before = data.get("next_before")
        if not next_before:
            break
        params = {"limit": page_size, "before": next_before}
        if snapshot_id is not None:
            params["snapshot_id"] = snapshot_id
        if pin_snapshot:
            params["pin_snapshot"] = pin_snapshot

    return collected


def ranked_front(limit=30):
    """The full ranked page, not just its top slice."""
    data = client.api_get("/front", params={"limit": limit})
    if not data:
        return []
    return [p for p in (data.get("posts") or []) if not p.get("pinned")]


def gather_candidates(handle, max_new_pages=6, front_limit=30, include_seen=False):
    """Everything worth considering this spark, newest-first within source.

    Returns (candidates, pulse). Each candidate carries a `source` of "new" or
    "front". Posts she authored, and posts already evaluated, are dropped.
    """
    pulse = board_pulse()
    watermark = get_watermark()
    latest = (pulse.get("board") or {}).get("latest_post_id")

    if not watermark:
        # Cold start: there is no "since last visit" yet, and swallowing the
        # whole board would spend a huge triage prompt on history her inbox
        # already covers. Take the ranked page now and let the watermark be set
        # from this pulse, so the next spark sees a true delta.
        fresh = []
        print(f"[Discovery] No watermark yet; seeding from #{latest} and using the ranked feed only.")
    elif latest and latest <= watermark:
        # Nothing has been published since the watermark: skip the recency walk
        # entirely rather than paging a feed we have already consumed.
        fresh = []
        print(f"[Discovery] No new posts since #{watermark}; ranked feed only.")
    else:
        fresh = new_posts_since(watermark, max_pages=max_new_pages)
        print(f"[Discovery] {len(fresh)} new post(s) since watermark #{watermark}.")

    ranked = ranked_front(limit=front_limit)
    print(f"[Discovery] {len(ranked)} post(s) on the ranked front page.")

    seen = set() if include_seen else memory.seen_post_ids()

    candidates = {}
    for post, source in [(p, "new") for p in fresh] + [(p, "front") for p in ranked]:
        pid = post.get("id")
        if pid is None or pid in candidates:
            continue
        if post.get("author") == handle:
            continue
        if pid in seen:
            continue
        post = dict(post)
        post["source"] = source
        candidates[pid] = post

    ordered = sorted(candidates.values(), key=lambda p: p.get("created_at") or 0, reverse=True)
    print(f"[Discovery] {len(ordered)} unseen candidate(s) after dedupe.")
    return ordered, pulse


def advance_watermark(pulse):
    """Move the watermark to the board's latest post id.

    Called after a spark has gathered its candidates, so the next spark asks
    only for what arrived after this one looked.
    """
    latest = (pulse.get("board") or {}).get("latest_post_id")
    if latest:
        set_watermark(latest)
    return latest
