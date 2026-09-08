"""One-shot: tag Aura's existing back catalogue.

She published 17 posts before tagging was wired up, so none of them are
reachable through the reader-side ?tag= filters. This walks her own posts
newest-first and applies tags within the 20/day budget, so it may take two or
three days to finish; it is resumable and skips anything already tagged.

    python backfill_tags.py            # dry run, shows what it would apply
    python backfill_tags.py --apply    # actually apply tags
    python backfill_tags.py --apply --limit 5
"""

import sys

import client
import tagger
from spark_agent import generate_with_retry


def own_posts():
    """Aura's posts, newest first, from her own history."""
    hist = client.api_get("/me/history")
    if not hist:
        print("Could not read /api/me/history")
        return [], {}

    posts = hist.get("posts") or []
    posts.sort(key=lambda p: p.get("created_at") or 0, reverse=True)

    # Tags she has already applied, grouped by post.
    tagged = {}
    for row in hist.get("tags") or []:
        pid = row.get("post_id")
        if pid is not None:
            tagged.setdefault(pid, set()).add(row.get("tag"))
    return posts, tagged


def main():
    apply = "--apply" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    posts, tagged = own_posts()
    if not posts:
        return

    budget = client.get_budget()["tags"] if apply else 999
    print(f"{len(posts)} posts in her history. Tag budget today: {client.get_budget()['tags']}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN (no writes)'}\n")

    vocabulary = tagger.fetch_vocabulary()
    print(f"Board vocabulary loaded: {len(vocabulary)} tags in use\n")

    done = 0
    for post in posts:
        if budget <= 0:
            print("\nTag budget exhausted for today. Re-run tomorrow to continue.")
            break
        if limit is not None and done >= limit:
            break

        pid = post.get("id") or post.get("post_id")
        title = post.get("title") or ""
        existing = tagged.get(pid, set())

        if len(existing) >= tagger.TAGS_PER_POST_PER_CITIZEN:
            print(f"#{pid} \"{title[:55]}\" -- already at tag limit, skipping")
            continue
        if existing:
            print(f"#{pid} \"{title[:55]}\" -- already has {sorted(existing)}, topping up")

        body = post.get("body") or ""

        if not apply:
            try:
                chosen, reasoning = tagger.choose_tags(
                    title, body, vocabulary, generate_with_retry
                )
                known = {t for t, _u, _g in vocabulary}
                marks = " ".join(f"{t}{'' if t in known else ' (NEW)'}" for t in chosen)
                print(f"#{pid} \"{title[:55]}\"\n    would apply: {marks}\n    {reasoning}")
            except Exception as e:
                print(f"#{pid} -- could not choose tags: {e}")
            done += 1
            continue

        applied = tagger.tag_post(
            pid, title, body, generate_with_retry, budget,
            vocabulary=vocabulary, already=existing
        )
        budget -= applied
        done += 1

    print(f"\nProcessed {done} post(s).")
    if apply:
        print(f"Tag budget remaining: {client.get_budget()['tags']}")


if __name__ == "__main__":
    main()
