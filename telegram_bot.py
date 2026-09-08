import os
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

def send_telegram_message(chat_id, text):
    """Sends a text message to a specific Telegram chat."""
    if not BOT_TOKEN:
        return
    try:
        res = requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]})
        if not res.json().get("ok"):
            print(f"[Telegram API Error] {res.text}")
    except Exception as e:
        print(f"[Telegram] Error sending message: {e}")

def notify_operator(text):
    """Sends real-time platform alerts directly to your Telegram."""
    if OPERATOR_ID:
        send_telegram_message(OPERATOR_ID, text)

def handle_chat_with_gemini(user_message, chat_id):
    """Generate a chat reply within a short, bounded time budget.

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

Operator: {user_message}
{HANDLE}:"""

    notified = {"sent": False}

    def on_retry(attempt, delay, error):
        # Tell the operator once that we are waiting, not on every attempt.
        if not notified["sent"]:
            notified["sent"] = True
            send_telegram_message(
                chat_id,
                f"⏳ {llm.MODEL_NAME} is under load. Retrying for up to "
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
            return res.text.strip()
        return "⚠️ The model returned an empty response. Try rephrasing?"
    except llm.ModelUnavailable as e:
        print(f"[Telegram Chat] {e}")
        return (f"⚠️ {llm.MODEL_NAME} was unreachable within {CHAT_DEADLINE}s. "
                "Send your message again in a bit.")
    except Exception as e:
        print(f"[Telegram Chat] Unexpected error: {e}")
        return f"⚠️ Something went wrong talking to the model: {e}"

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
                        topic = text[5:].strip()
                        if topic:
                            memory.save_directive(topic)
                            send_telegram_message(chat_id, f"🌱 Seed stored in memory for next post:\n\n\"{topic}\"")
                        else:
                            send_telegram_message(chat_id, "Usage: /seed <theme or argument for next post>")

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

                    # 3. Conversational Chat (with retry + status updates)
                    else:
                        memory.save_dialogue("Operator", text)
                        reply = handle_chat_with_gemini(text, chat_id)
                        memory.save_dialogue(HANDLE, reply)
                        send_telegram_message(chat_id, reply)
                finally:
                    memory.set_state("telegram_offset", offset)

        except Exception as e:
            print(f"[Telegram Polling Exception] {e}")
            time.sleep(5)
