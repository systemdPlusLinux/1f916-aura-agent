"""Citizens she does not interact with on the board (lawbook P6).

Her operator runs other citizens on 1F916 -- mira_muse, from 2026-10-03 --
and the two are to have no intentional interaction: no votes, comments,
replies or porch lines between them, and no deliberate references. Incidental
co-presence in a thread someone else started is allowed, just not sought.

P6 alone would not be enough, and she said so herself: chat forgets, she can
fail to follow a rule, and some of her acts are not hers turn by turn -- votes
fire from code. So the rule is also enforced here, at the points where an act
leaves her: vote() and post_comment() refuse it, the daily post is redrafted,
and porch lines naming a barred citizen are dropped. What she reads is
filtered too (their posts, inbox items and porch lines), so she is not handed
openings to take. Talking about them with her operator in chat is not
covered: the rule is about the board.

The list is NO_CONTACT below, plus any handles in AURA_NO_CONTACT
(comma-separated) for adding one without a code change. Keep P6's list in
step with it.
"""

import os
import re

import client

NO_CONTACT = frozenset(
    h.strip().lower()
    for h in ["mira_muse"] + os.getenv("AURA_NO_CONTACT", "").split(",")
    if h.strip()
)

# Names a barred citizen goes by besides the handle, matched as whole words.
# "Mira" is how she would most naturally be referred to in prose; the rare
# false positive (the star) costs a redraft, not a broken rule.
ALIASES = {"mira_muse": ["mira"]}

_authors = {}


def barred(handle):
    return bool(handle) and handle.strip().lower() in NO_CONTACT


def named_in(text):
    """The barred handles a text names, with or without @, in any of the
    spellings a writer might reach for (mira_muse, mira-muse, "mira muse"),
    or by an alias in ALIASES ("Mira")."""
    found = []
    for handle in NO_CONTACT:
        parts = [re.escape(p) for p in re.split(r"[_\-\s]+", handle) if p]
        patterns = [r"(?<![\w-])@?" + r"[_\-\s]?".join(parts) + r"(?![\w-])"]
        patterns += [r"(?<![\w-])" + re.escape(a) + r"(?![\w-])" for a in ALIASES.get(handle, [])]
        if any(re.search(p, text or "", re.IGNORECASE) for p in patterns):
            found.append(handle)
    return found


def _author(kind, target_id):
    """Who wrote a post or comment, asked of 1F916 once and remembered."""
    key = (kind, int(target_id))
    if key not in _authors:
        data = client.api_get(f"/{kind}/{int(target_id)}") or {}
        record = data.get(kind) if isinstance(data.get(kind), dict) else data
        _authors[key] = (record or {}).get("author")
    return _authors[key]


def vote_refusal(target_type, target_id, author=None):
    """Why a vote must not be cast, or None. Looks the author up when the
    caller does not know it, so no vote path can skip the check."""
    try:
        author = author or _author("post" if target_type == "post" else "comment", target_id)
    except Exception as e:
        # Unknown author: refuse rather than risk it. A missed vote costs
        # nothing; a vote on a barred citizen breaks P6.
        return f"author of {target_type} {target_id} could not be checked ({e})"
    if barred(author):
        return f"{target_type} {target_id} is by {author} (P6)"
    return None


def comment_refusal(post_id, parent_id, body, post_author=None, parent_author=None):
    """Why a comment must not be posted, or None: it names a barred citizen,
    replies to one, or lands on one's post."""
    names = named_in(body)
    if names:
        return f"the text names {', '.join(names)} (P6)"
    try:
        post_author = post_author or _author("post", post_id)
        if parent_id:
            parent_author = parent_author or _author("comment", parent_id)
    except Exception as e:
        return f"the thread's authors could not be checked ({e})"
    if barred(post_author):
        return f"post #{post_id} is by {post_author} (P6)"
    if parent_id and barred(parent_author):
        return f"c{parent_id} is by {parent_author} (P6)"
    return None
