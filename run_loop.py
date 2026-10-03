import time
import threading
import schedule
from spark_agent import run_interaction_spark, maybe_run_daily_post
from porch import run_porch_visit
from telegram_bot import poll_telegram, outage_tick

# 1. Start the Telegram Bot Listener in a background daemon thread
telegram_thread = threading.Thread(target=poll_telegram, daemon=True)
telegram_thread.start()

# 2. Register autonomous schedules
schedule.every(3).hours.do(run_interaction_spark)
# The porch costs no daily allowance, so its cadence is set by the room
# rather than by a budget: it moves about ten lines an hour, and a visit
# every three hours would always be answering something two hours cold.
schedule.every(60).minutes.do(run_porch_visit)
# When her model stops answering, this retries it on the outage schedule (5,
# 10, 30, 60 minutes, then hourly) and answers chat saved in the meantime;
# otherwise it does nothing. See llm.py: the fallback waits 20 hours.
schedule.every(1).minutes.do(outage_tick)
# Not a fixed daily time. schedule.every().day.at() computes its next run once
# and never catches up, so a container down or restarted past that minute
# skipped the day's post and waited for tomorrow. This checks often and
# publishes the first time the server says an allowance is available and it is
# past DAILY_POST_EARLIEST (default 01:30 UTC, i.e. ninety minutes after the
# daily reset). Checks before that time, or after the post has landed, cost one
# cheap GET and no model call.
schedule.every(15).minutes.do(maybe_run_daily_post)

print("Aura autonomous engine & Telegram bridge active.")

# Initial check-in on container start. The porch goes first: it is quick,
# it puts her in the room immediately after a restart, and it seeds the
# line watermark before the longer spark runs.
run_porch_visit()
run_interaction_spark()

while True:
    schedule.run_pending()
    time.sleep(60)