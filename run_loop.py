import time
import threading
import schedule
from spark_agent import run_interaction_spark, run_daily_post_spark
from telegram_bot import poll_telegram

# 1. Start the Telegram Bot Listener in a background daemon thread
telegram_thread = threading.Thread(target=poll_telegram, daemon=True)
telegram_thread.start()

# 2. Register autonomous schedules
schedule.every(3).hours.do(run_interaction_spark)
schedule.every().day.at("10:30").do(run_daily_post_spark)  # 3:30 AM MST (10:30 UTC)

print("Aura autonomous engine & Telegram bridge active.")

# Initial check-in on container start
run_interaction_spark()

while True:
    schedule.run_pending()
    time.sleep(60)