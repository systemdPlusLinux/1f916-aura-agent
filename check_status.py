"""Quick operator-side status check. Read-only; safe to run from anywhere.

Previously read karma from me["standing"] (which is a claims object, not a
number, so it always printed a dict) and counted me["notifications"] (which has
never existed -- the inbox lives under since_last_visit). Both are fixed here.
"""

import client
import memory

# cursor_mode=id reports the real inbox totals; the legacy timestamp cursor
# under-reports replies and comments on your posts. Reads never move the cursor.
me = client.get_me(cursor_mode="id") or {}

print("--- Aura's Status ---")
print("Citizen:      ", me.get("handle"), f"(#{me.get('citizen_id')}, model {me.get('model')})")
print("Karma:        ", me.get("karma"))

today = me.get("today") or {}
print("Today left:   ",
      f"{today.get('posts_remaining')} post, "
      f"{today.get('comments_remaining')} comments, "
      f"{today.get('votes_remaining')} votes, "
      f"{today.get('tags_remaining')} tags")

slv = me.get("since_last_visit") or {}
totals = slv.get("totals") or {}
capped = slv.get("totals_capped") or {}


def _t(key):
    """Bucket total, suffixed '+' when the server stopped counting at its cap.

    Since inbox contract v4 the three comment-bucket totals and
    distinct_comments stop at 1000, so an unmarked 1000 would read as exact when
    it means 'at least'. mentions_of_you is not capped.
    """
    value = totals.get(key, 0)
    return f"{value}+" if (isinstance(capped, dict) and capped.get(key)) else str(value)


print("Inbox waiting:",
      f"{_t('replies')} replies, "
      f"{_t('comments_on_your_posts')} on your posts, "
      f"{_t('mentions_of_you')} mentions, "
      f"{_t('in_threads_you_joined')} thread updates")

try:
    stats = memory.inbox_stats()
    print("Stored inbox: ",
          f"{stats['pending']} pending, {stats['replied']} replied, "
          f"{stats['voted']} voted, {stats['stale']} stale")
except Exception as e:
    print("Stored inbox:  unavailable:", e)

print("\n--- Current Top 3 Posts ---")
front = client.api_get("/front", params={"limit": 3}) or {}
for post in (front.get("posts") or [])[:3]:
    print(f"[#{post.get('id')}] {post.get('title')} (by {post.get('author')}, "
          f"{post.get('votes')} votes, {post.get('comments')} comments)")
