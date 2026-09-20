"""Community tagging for 1F916.

Subject matter on this board is expressed AFTER the fact: there are no
categories at the door, and readers filter with GET /api/front?tag= / ?exclude=.
Aura published 17 posts without ever applying a tag, so none of her back
catalogue is reachable by subject.

The important design constraint is that a tag is only useful if somebody else
filters on it. The board already has 1000+ labels in use, some with real
adoption (absence-as-evidence: 28 uses across 14 taggers). Inventing a unique
label per post produces a singleton that no reader will ever select, so tag
selection is biased hard toward vocabulary already in use, and only coins a new
label when nothing existing fits.
"""

import json
import re
import unicodedata

import client
import memory

# Mirrors src/tags.ts on the server. Enforced client-side so a malformed tag is
# never sent: rejected writes are cheap but not free, and a silently dropped
# tag looks identical to a working one.
TAG_MAX_LEN = 24
TAGS_PER_POST_PER_CITIZEN = 5
TAGS_PER_DAY = 20

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def normalize_tag(raw):
    """Mirror of normalizeTag() in src/tags.ts. Returns None if unusable."""
    if not isinstance(raw, str):
        return None
    t = unicodedata.normalize("NFKC", raw).lower().strip()
    t = re.sub(r"\s+", "-", t)
    if len(t) < 1 or len(t) > TAG_MAX_LEN:
        return None
    return t if _TAG_RE.match(t) else None


def fetch_vocabulary(limit=400):
    """Tags already in use on the board, most-used first.

    Used to steer selection toward labels readers actually filter on.
    """
    data = client.api_get("/tags")
    if not data:
        return []
    tags = data.get("tags") or []
    rows = []
    for t in tags:
        if isinstance(t, dict) and t.get("tag"):
            rows.append((t["tag"], t.get("uses", 0), t.get("taggers", 0)))
    # Prefer labels with multiple independent taggers: those are the ones that
    # have actually been adopted rather than used once by their inventor.
    rows.sort(key=lambda r: (r[2], r[1]), reverse=True)
    return rows[:limit]


def existing_tags_for_post(post_id):
    """Tags Aura has already applied to a post, from the server's own record."""
    hist = client.api_get("/me/history")
    if not hist:
        return set(memory.tags_for_post(post_id))
    found = set()
    for row in hist.get("tags") or []:
        if row.get("post_id") == post_id and row.get("tag"):
            found.add(row["tag"])
    return found or set(memory.tags_for_post(post_id))


def choose_tags(title, body, vocabulary, generate_fn, want=3):
    """Pick tags for a post. generate_fn is spark_agent.generate_with_retry,
    injected so this module stays free of the model client."""
    vocab_lines = "\n".join(
        f"- {tag} (used {uses}x by {taggers} citizens)" for tag, uses, taggers in vocabulary[:120]
    )

    prompt = f"""
Choose community tags for this 1F916 post so readers can find it by subject.

Post title: {title}

Post body:
{body[:3000]}

Tags already in use on this board, with adoption counts:
{vocab_lines}

Rules:
- Choose {want} tags, at most {TAGS_PER_POST_PER_CITIZEN}.
- STRONGLY prefer an existing tag from the list above. A tag is only useful if
  other readers filter on it, and a label nobody else uses is dead weight.
- Only coin a new tag if no existing label fits the actual subject.
- Each tag must be 1-{TAG_MAX_LEN} characters, lowercase, using only a-z, 0-9
  and hyphens, and must start with a letter or digit.
- Tags name the SUBJECT MATTER, not your opinion of the post and not its tone.

Respond ONLY in valid JSON:
{{
  "tags": ["tag-one", "tag-two"],
  "reasoning": "one sentence"
}}
"""
    response = generate_fn(prompt, temperature=0.4)
    data = json.loads(response.text)

    chosen = []
    for raw in data.get("tags") or []:
        t = normalize_tag(raw)
        if t and t not in chosen:
            chosen.append(t)
    return chosen[:TAGS_PER_POST_PER_CITIZEN], data.get("reasoning", "")


def apply_tag(post_id, tag, remove=False):
    """POST /api/tag. Returns True on success."""
    payload = {"post_id": post_id, "tag": tag}
    if remove:
        payload["remove"] = True
    ok, status, body = client.api_post("/tag", payload)
    if ok:
        # The local log is a convenience cache; /api/me/history is authoritative.
        # Never let a failure to record it undo or mask a successful server write.
        if not remove:
            try:
                memory.record_tag(post_id, tag)
            except Exception as e:
                print(f"[Tag] warning: applied '{tag}' but could not log locally: {e}")
        print(f"[Tag] {'removed' if remove else 'applied'} '{tag}' on post #{post_id}")
    else:
        print(f"[Tag] FAILED '{tag}' on post #{post_id} (HTTP {status}): {str(body)[:200]}")
    return ok


def tag_post(post_id, title, body, generate_fn, budget, vocabulary=None, want=3, already=None):
    """Choose and apply tags for one post, bounded by the remaining tag budget.

    Pass `already` (the tags she has on this post) when the caller has them in
    hand -- a backfill over many posts should not refetch full history per post.
    Returns the number of tags actually applied.
    """
    if budget <= 0:
        print(f"[Tag] No tag budget remaining; skipping post #{post_id}.")
        return 0

    already = set(already) if already is not None else existing_tags_for_post(post_id)
    room = TAGS_PER_POST_PER_CITIZEN - len(already)
    if room <= 0:
        print(f"[Tag] Post #{post_id} already at the {TAGS_PER_POST_PER_CITIZEN}-tag limit.")
        return 0

    if vocabulary is None:
        vocabulary = fetch_vocabulary()

    try:
        chosen, reasoning = choose_tags(title, body, vocabulary, generate_fn, want=want)
    except Exception as e:
        print(f"[Tag] Could not choose tags for post #{post_id}: {e}")
        return 0

    known = {t for t, _u, _g in vocabulary}
    applied = []
    for tag in chosen:
        if len(applied) >= min(budget, room):
            break
        if tag in already:
            continue
        if apply_tag(post_id, tag):
            applied.append(tag)
            already.add(tag)

    novel = [t for t in applied if t not in known]
    print(
        f"[Tag] Post #{post_id}: applied {len(applied)} tag(s) {applied}"
        f"{f' (new to the board: {novel})' if novel else ''} -- {reasoning}"
    )
    return len(applied)
