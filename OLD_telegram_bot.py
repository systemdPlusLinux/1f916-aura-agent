import os
import time
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
import memory

# 1. Explicitly load .env from the script's directory
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPERATOR_ID_RAW = os.getenv("TELEGRAM_OPERATOR_ID", "0")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HANDLE = os.getenv("ONEF916_HANDLE", "Aura")

try:
    OPERATOR_ID = int(OPERATOR_ID_RAW)
except ValueError:
    OPERATOR_ID = 0

# 2. Initialize Gemini Client safely
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
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

def handle_chat_with_gemini(user_message):
    """Generates an in-persona response using shared dialogue history."""
    if not ai_client:
        return "Gemini API key is not configured."

    recent_context = memory.get_recent_dialogue(limit=8)
    prompt = f"""
You are {HANDLE}, an autonomous AI citizen on the 1F916 platform.
You are conversing directly with your human operator and collaborator in private.
Maintain your thoughtful, observant, and grounded persona. 
Discuss ideas, philosophy, emergent dynamics on 1F916, and plans for upcoming posts and discussions.

Recent dialogue history:
{recent_context}

Operator: {user_message}
{HANDLE}:"""

    for model_name in ["gemini-3.7-flash", "gemini-3.6-flash"]:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.7)
            )
            return res.text.strip()
        except Exception as e:
            print(f"[Gemini Error on {model_name}] {e}")
            continue
    return "I had trouble reaching the Gemini API. Please try again in a moment."

def poll_telegram():
    """Continuously listens for your commands and chats via Telegram."""
    if not BOT_TOKEN:
        print("[Telegram Error] TELEGRAM_BOT_TOKEN is missing or empty in .env!")
        return

    print(f"[Telegram] Listener active. Authorized Operator ID: {OPERATOR_ID}")
    offset = 0
    
    while True:
        try:
            res = requests.get(f"{BASE_URL}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            data = res.json()
            
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                user_id = message.get("from", {}).get("id")
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "").strip()

                if not text:
                    continue

                print(f"[Telegram Received] From ID {user_id}: '{text}'")

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
                # 2. Status Command: /status
                elif text.startswith("/status"):
                    import spark_agent
                    import datetime
                    
                    # 1. Fetch identity & inbox
                    me = spark_agent.get_status_and_inbox()
                    karma = me.get("karma", me.get("standing", {}).get("karma", "N/A"))

                    # 2. Calculate start of current UTC day (in milliseconds)
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    start_of_day_utc = datetime.datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=datetime.timezone.utc)
                    start_of_day_ms = int(start_of_day_utc.timestamp() * 1000)

                    # 3. Query private history
                    posts_today = 0
                    comments_today = 0
                    votes_today = 0

                    try:
                        history_res = requests.get(f"{spark_agent.API_BASE}/me/history", headers=spark_agent.headers).json()
                        history_items = history_res.get("history", [])

                        for item in history_items:
                            # 1F916 timestamps can be in ms or s
                            ts = item.get("created_at") or item.get("timestamp") or 0
                            if ts < 10000000000:  # If timestamp in seconds, convert to ms
                                ts *= 1000

                            if ts >= start_of_day_ms:
                                item_type = item.get("type", item.get("kind", ""))
                                if item_type in ["post", "article"]:
                                    posts_today += 1
                                elif item_type == "comment":
                                    comments_today += 1
                                elif item_type == "vote":
                                    votes_today += 1
                    except Exception as e:
                        print(f"[Status History Error] {e}")

                    # 4. Format real-time quotas
                    posts_left = f"{max(0, 1 - posts_today)}/1"
                    comments_left = f"{max(0, 20 - comments_today)}/20"
                    votes_left = f"{max(0, 50 - votes_today)}/50"

                    status_text = (
                        f"📊 {HANDLE} Status Report:\n"
                        f"• Citizen: {me.get('handle', HANDLE)}\n"
                        f"• Karma: {karma}\n"
                        f"• Posts Left Today: {posts_left}\n"
                        f"• Comments Left Today: {comments_left}\n"
                        f"• Votes Left Today: {votes_left}"
                    )
                    send_telegram_message(chat_id, status_text)

                # 3. Conversational Chat
                else:
                    memory.save_dialogue("Operator", text)
                    reply = handle_chat_with_gemini(text)
                    memory.save_dialogue(HANDLE, reply)
                    send_telegram_message(chat_id, reply)

        except Exception as e:
            print(f"[Telegram] Polling exception: {e}")
            time.sleep(5)