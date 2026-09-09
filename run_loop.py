import time
import threading
import schedule
from spark_agent import run_interaction_spark, run_daily_post_spark
from porch import run_porch_visit
from telegram_bot import poll_telegram

# 1. Start the Telegram Bot Listener in a background daemon thread
telegram_thread = threading.Thread(target=poll_telegram, daemon=True)
telegram_thread.start()

# 2. Register autonomous schedules
schedule.every(3).hours.do(run_interaction_spark)
# The porch costs no daily allowance, so its cadence is set by the room
# rather than by a budget: it moves about ten lines an hour, and a visit
# every three hours would always be answering something two hours cold.
schedule.every(60).minutes.do(run_porch_visit)
schedule.every().day.at("10:30").do(run_daily_post_spark)  # 3:30 AM MST (10:30 UTC)

print("Aura autonomous engine & Telegram bridge active.")

# Initial check-in on container start. The porch goes first: it is quick,
# it puts her in the room immediately after a restart, and it seeds the
# line watermark before the longer spark runs.
run_porch_visit()
run_interaction_spark()

while True:
    schedule.run_pending()
    time.sleep(60)