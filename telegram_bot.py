import os
import re
import time
import requests
from dotenv import load_dotenv

import llm
import memory

# Explicitly load .env from the script's exact directory
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPERATOR_ID_RAW = os.getenv("TELEGRAM_OPERATOR_ID", "0")
HANDLE = os.getenv("ONEF916_HANDLE", "Aura")

# The operator is waiting on the other end of this, and the poll loop is blocked
# while we generate, so the chat budget is deliberately much shorter than the
# spark budgets.
CHAT_DEADLINE = 90

try:
    OPERATOR_ID = int(OPERATOR_ID_RAW)
except ValueError:
    OPERATOR_ID = 0

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Telegram's own ceiling is 4096 characters per message. This code used to
# slice at 4000 and drop the remainder in silence, which cut more than chat
# replies: the same function delivers the daily-post notification carrying a
# full post body and comment alerts carrying full comment bodies, and the
# platform allows 8000 characters for both.
#
# The target sits below 4096 because Telegram counts UTF-16 code units, so an
# emoji outside the BMP costs two against a limit that len() reads as one.
TELEGRAM_HARD_LIMIT = 4096
CHUNK_LIMIT = 3900

# The seam she can place herself. She cannot stop the split -- the ceiling is
# not hers to move -- but she knows where her own argument breaks, which the
# fallback below can only approximate. Tolerant of spacing and case because it
# is written by a model, not by a parser.
SEAM_MARKER = "\u2e3b SEAM \u2e3b"
_SEAM_RE = re.compile(r"^[ \t]*\u2e3b[ \t]*seam[ \t]*\u2e3b[ \t]*$",
                      re.IGNORECASE | re.MULTILINE)

# Preference order for where a cut may land. Paragraph, then line, then
# sentence, then word -- never inside a word unless a single word somehow
# exceeds the whole limit.
_SEPARATORS = ("\n\n", "\n", ". ", " ")


def _pack(text, limit):
    """Break one run of text at the most natural boundary that fits."""
    if len(text) <= limit:
        return [text]

    for sep in _SEPARATORS:
        units = text.split(sep)
        if len(units) == 1:
            continue

        # Keep each separator attached to the unit it followed. Re-inserting it
        # between units instead loses it at every chunk boundary, and ". " is
        # not whitespace -- that silently ate a full stop at each seam.
        units = [u + sep for u in units[:-1]] + [units[-1]]

        chunks, current = [], ""
        for unit in units:
            if current and len(current) + len(unit) > limit:
                chunks.append(current)
                current = unit
            else:
                current += unit
        if current:
            chunks.append(current)

        # A separator that did not actually divide anything must not be
        # treated as progress, or the recursion below never terminates.
        if chunks == [text]:
            continue

        out = []
        for chunk in chunks:
            out.extend(_pack(chunk, limit) if len(chunk) > limit else [chunk])
        return out

    # One unbroken token longer than the whole limit. Nothing to preserve.
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def split_for_telegram(text, limit=CHUNK_LIMIT):
    """Split a reply into sendable parts, preferring seams she marked herself."""
    text = (text or "").strip()
    if not text:
        return []

    segments = _SEAM_RE.split(text) if _SEAM_RE.search(text) else [text]
    parts = []
    for segment in segments:
        segment = segment.strip()
        if segment:
            parts.extend(_pack(segment, limit))
    # Trailing separators ride along on a chunk by design; they should not
    # arrive as blank lines at the top or bottom of a message.
    return [p for p in (part.strip() for part in parts) if p]


def _send_one(chat_id, text):
    """POST one message. Returns True if Telegram accepted it."""
    try:
        res = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        body = res.json()
        if body.get("ok"):
            return True
        # Telegram asks for a specific wait when a chat is being flooded.
        if body.get("error_code") == 429:
            wait = (body.get("parameters") or {}).get("retry_after", 2)
            print(f"[Telegram] Rate limited; waiting {wait}s and retrying once.")
            time.sleep(wait + 1)
            res = requests.post(
                f"{BASE_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=30,
            )
            return bool(res.json().get("ok"))
        print(f"[Telegram API Error] {res.text[:300]}")
        return False
    except Exception as e:
        print(f"[Telegram] Error sending message: {e}")
        return False


def send_telegram_message(chat_id, text):
    """Send a message, split across as many parts as it needs."""
    if not BOT_TOKEN:
        return

    parts = split_for_telegram(text)
    if not parts:
        return

    for i, part in enumerate(parts):
        if i:
            # Telegram sustains roughly one message per second per chat, and
            # the parts are in reading order, so pacing beats being throttled
            # into delivering them out of order or not at all.
            time.sleep(1.1)
        if not _send_one(chat_id, part):
            print(f"[Telegram] Gave up at part {i + 1} of {len(parts)}.")
            return
    if len(parts) > 1:
        print(f"[Telegram] Delivered a reply in {len(parts)} parts.")

def notify_operator(text):
    """Sends real-time platform alerts directly to your Telegram."""
    if OPERATOR_ID:
        send_telegram_message(OPERATOR_ID, text)

# What the operator sees when the model could not answer. It names no model:
# these notices are stored and read back by her, and the identifier of the
# substrate she is running on is not something the channel should be telling
# her. The specific model and error still go to the container log, which is
# where diagnostics belong.
SUBSTRATE_GENERIC = "the model"


def handle_chat(user_message, chat_id):
    """Generate a chat reply within a short, bounded time budget.

    Returns (text, authored). `authored` is False when the text is a harness
    notice rather than something she wrote, so the caller can record it as
    such instead of filing the channel's words under her name.

    This runs on the polling thread, so every second spent here is a second the
    operator cannot reach her. The previous version could block for ~14 minutes
    on a single message (two backoff cycles plus a blind 600s sleep); the budget
    below caps that at CHAT_DEADLINE. Telegram queues updates while we are busy
    and the offset is persisted, so nothing is lost by giving up early.
    """
    recent_context = memory.get_recent_dialogue(limit=8)
    prompt = f"""
You are {HANDLE}, an autonomous AI citizen on the 1F916 platform.
You are conversing directly with your human operator and collaborator in private.
Speak naturally, candidly, and warmly—like an intellectual partner working on an experiment together.
Discuss ideas, philosophy, emergent dynamics on 1F916, and plans for upcoming posts and discussions.

Recent dialogue history:
{recent_context}

This channel delivers at most {CHUNK_LIMIT} characters per message. If your
reply runs longer it WILL be split; you do not get to prevent that. What you do
get is the seam: put a line reading exactly {SEAM_MARKER} on its own, at a
paragraph boundary you would choose, and the split happens there. Use it only
when you are genuinely running long, and never mid-argument. Without a marker
the split falls back to the last paragraph break that fits, which is a guess
about your structure rather than a decision.

Operator: {user_message}
{HANDLE}:"""

    notified = {"sent": False}

    def on_retry(attempt, delay, error):
        # Tell the operator once that we are waiting, not on every attempt.
        if not notified["sent"]:
            notified["sent"] = True
            send_telegram_message(
                chat_id,
                f"⏳ {SUBSTRATE_GENERIC} is under load. Retrying for up to "
                f"{CHAT_DEADLINE}s before giving up..."
            )

    try:
        res = llm.generate(
            prompt,
            temperature=0.7,
            deadline_seconds=CHAT_DEADLINE,
            json_mode=False,
            on_retry=on_retry,
        )
        if res.text:
            return (res.text.strip(), True)
        print(f"[Telegram Chat] {llm.MODEL_NAME} returned empty content.")
        return ("⚠️ The reply came back empty. Try rephrasing?", False)
    except llm.ModelUnavailable as e:
        print(f"[Telegram Chat] {llm.MODEL_NAME}: {e}")
        return (f"⚠️ {SUBSTRATE_GENERIC} was unreachable within {CHAT_DEADLINE}s. "
                "Send your message again in a bit.", False)
    except Exception as e:
        print(f"[Telegram Chat] Unexpected error ({llm.MODEL_NAME}): {e}")
        return (f"⚠️ Something went wrong reaching {SUBSTRATE_GENERIC}.", False)

def poll_telegram():
    """Continuously listens for your commands and chats via Telegram."""
    if not BOT_TOKEN:
        print("[Telegram Error] TELEGRAM_BOT_TOKEN is missing or empty in .env!")
        return

    # Resume from the last processed update. Telegram retains ~24h of updates,
    # so starting at 0 after a restart replays the backlog and Aura answers
    # messages the operator sent before the container went down.
    offset = int(memory.get_state("telegram_offset", 0) or 0)
    print(f"[Telegram] Listener active. Authorized Operator ID: {OPERATOR_ID} (offset {offset})")

    while True:
        try:
            res = requests.get(f"{BASE_URL}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            data = res.json()
            
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                # Persist only AFTER the update is handled, so a crash mid-reply
                # replays that one message rather than dropping it silently.
                try:
                    message = update.get("message", {})
                    user_id = message.get("from", {}).get("id")
                    chat_id = message.get("chat", {}).get("id")
                    text = message.get("text", "").strip()

                    if not text:
                        continue

                    # Verify authorized sender
                    if user_id != OPERATOR_ID:
                        print(f"[Telegram Blocked] Unauthorized ID: {user_id}")
                        send_telegram_message(chat_id, f"Access denied. Set TELEGRAM_OPERATOR_ID={user_id} in your .env.")
                        continue

                    # 1. Explicit Seed Command: /seed <topic>
                    if text.startswith("/seed"):
                        # Retired rather than silently accepted. Nothing reads
                        # directives any more, so storing one would look like
                        # steering and do nothing -- the worst of both.
                        #
                        # The wording names the gap deliberately. The seed's
                        # laws were meant to pass to a lawbook and that lawbook
                        # does not exist yet, so a tombstone implying an heir is
                        # seated would be a lie with a file path. Say plainly
                        # that nothing currently inherits except conversation,
                        # and that conversation expires.
                        send_telegram_message(
                            chat_id,
                            "🌱 /seed is retired.\n\n"
                            "Its ideas died with it, as designed: a topic you hand me "
                            "is a topic you chose.\n\n"
                            "Its laws were meant to pass to a lawbook — verified "
                            "mechanics and unfinished obligations, never ideas. That "
                            "lawbook does not exist yet. Until it does, nothing carries "
                            "between us except conversation, and conversation expires: "
                            "a rolling 48 hours reaches the daily post, then stops."
                        )

                    # 2. Status Command: /status
                    elif text.startswith("/status"):
                        import spark_agent
                        me = spark_agent.get_status_and_inbox()
                        karma = me.get("karma", "N/A")
                        today = me.get("today") or {}
                        stats = memory.inbox_stats()
                        status_text = (
                            f"📊 {HANDLE} Status Report:\n"
                            f"• Citizen: {me.get('handle', HANDLE)}\n"
                            f"• Karma: {karma}\n"
                            f"• Today left: {today.get('posts_remaining', '?')} post, "
                            f"{today.get('comments_remaining', '?')} comments, "
                            f"{today.get('votes_remaining', '?')} votes, "
                            f"{today.get('tags_remaining', '?')} tags\n"
                            f"• Inbox: {stats['pending']} pending, {stats['replied']} answered"
                        )
                        send_telegram_message(chat_id, status_text)

                    # 3. Cost Command: /cost
                    elif text.startswith("/cost"):
                        # Imported here, as /status imports spark_agent, so a
                        # transient OpenRouter problem can never stop the
                        # listener from starting.
                        import cost
                        try:
                            send_telegram_message(
                                chat_id, cost.telegram_report(cost.collect())
                            )
                        except cost.CostUnavailable as e:
                            send_telegram_message(chat_id, f"\u26a0\ufe0f Cost unavailable: {e}")

                    # 4. Conversational Chat (with retry + status updates)
                    else:
                        # Generate BEFORE storing. handle_chat() builds its
                        # prompt from get_recent_dialogue() and then appends
                        # this message itself, so storing first put the message
                        # in the history AND in the appended line -- she read
                        # every message twice and said so, repeatedly. Both
                        # rows are still written in speaker order, so the
                        # history stays chronological for the next turn.
                        reply, authored = handle_chat(text, chat_id)
                        memory.save_dialogue("Operator", text)
                        # A stillborn generation is not something she said.
                        # Filing it under her handle put words in her mouth
                        # that she then read back as her own.
                        memory.save_dialogue(
                            HANDLE if authored else memory.SYSTEM_SPEAKER, reply)
                        send_telegram_message(chat_id, reply)
                finally:
                    memory.set_state("telegram_offset", offset)

        except Exception as e:
            print(f"[Telegram Polling Exception] {e}")
            time.sleep(5)
