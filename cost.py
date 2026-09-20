"""What she actually costs to run. Read-only; safe to run from anywhere.

This exists because the first cost figure in this project was a projection
extrapolated from a handful of calls, and a projection is exactly the kind of
number that gets quoted back later as if it were measured. OpenRouter meters
the key itself, so there is no need to estimate: `usage_daily` is the answer
and everything here is derived from it.

`collect()` does the fetching and arithmetic; the two report functions only
format. That split exists because this is rendered in a terminal AND over
Telegram, and the fixed-width alignment that reads well in one reads badly in
the other -- but the numbers behind them must never be allowed to diverge.

The one judgement call is the runway projection. It scales the day so far up to
a whole day, which assumes she was awake for all of it. The reports say so,
because the first time this ran it projected $0.53/month off ten minutes of
uptime -- a figure that would have looked like a measurement a week later.
"""

import datetime
import sys

import requests

import llm

TIMEOUT = 25

# What the scheduler fires, from run_loop.py. Reported so the per-event figure
# can be read against the cadence that produces it.
PORCH_VISITS_PER_DAY = 24
SPARKS_PER_DAY = 8
POSTS_PER_DAY = 1
EVENTS_PER_DAY = PORCH_VISITS_PER_DAY + SPARKS_PER_DAY + POSTS_PER_DAY

# Below this much of a UTC day, scaling up to 24 hours turns one expensive call
# into a wild month, so no projection is offered at all.
MIN_DAY_FRACTION = 0.25


class CostUnavailable(Exception):
    """The meter could not be read. Never raises SystemExit: this runs on the
    Telegram polling thread too, and exiting there would kill the listener."""


def _get(path):
    try:
        res = requests.get(
            f"{llm.API_BASE}{path}",
            headers={"Authorization": f"Bearer {llm.OPENROUTER_API_KEY}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise CostUnavailable(f"could not reach OpenRouter: {e}")
    if res.status_code == 401:
        raise CostUnavailable("OpenRouter rejected the key (401). Check OPENROUTER_API_KEY.")
    if res.status_code != 200:
        raise CostUnavailable(f"GET {path} -> HTTP {res.status_code}: {res.text[:160]}")
    return (res.json() or {}).get("data") or {}


def money(value):
    """Sub-cent sums are the normal case here, so two decimals would read $0.00."""
    if value is None:
        return "n/a"
    return f"${value:.6f}" if value < 0.01 else f"${value:.4f}"


def collect():
    """Fetch the meter and derive everything both reports render."""
    if not llm.OPENROUTER_API_KEY:
        raise CostUnavailable("OPENROUTER_API_KEY is not set.")

    key = _get("/key")
    credits = _get("/credits")
    now = datetime.datetime.now(datetime.UTC)
    day_fraction = (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0

    total = credits.get("total_credits")
    used = credits.get("total_usage")
    daily = key.get("usage_daily")

    d = {
        "model": llm.MODEL_NAME,
        "label": key.get("label"),
        "now": now,
        "day_fraction": day_fraction,
        "daily": daily,
        "weekly": key.get("usage_weekly"),
        "monthly": key.get("usage_monthly"),
        "all_time": key.get("usage"),
        "credits_total": total,
        "credits_used": used,
        "credits_left": (total - used) if (total is not None and used is not None) else None,
        "limit": key.get("limit"),
        "limit_reset": key.get("limit_reset"),
        "limit_remaining": key.get("limit_remaining"),
        "per_day": None,
        "per_30": None,
        "per_event": None,
        "runway_days": None,
        "no_projection": None,
    }

    if not daily:
        d["no_projection"] = "nothing metered today yet"
    elif day_fraction < MIN_DAY_FRACTION:
        d["no_projection"] = (
            f"only {day_fraction * 100:.0f}% of the UTC day has elapsed -- "
            "too little to project from"
        )
    else:
        per_day = daily / day_fraction
        d["per_day"] = per_day
        d["per_30"] = per_day * 30
        d["per_event"] = per_day / EVENTS_PER_DAY
        if d["credits_left"] is not None and per_day > 0:
            d["runway_days"] = d["credits_left"] / per_day
    return d


# The assumption that decides whether the projection means anything. Scaling
# the day's spend by elapsed time silently assumes she was running for all of
# it, so any downtime divides real spend across hours she was not awake and
# reads as a cheaper agent than she is.
UPTIME_CAVEAT = "Assumes she ran all of that window; a restart today makes it an undercount."


def terminal_report(d):
    out = ["--- Aura's model spend ---",
           f"Model:         {d['model']}",
           f"Key:           {d['label']}",
           f"As of:         {d['now']:%Y-%m-%d %H:%M UTC}",
           "",
           "Metered by OpenRouter (not estimated):",
           f"  today:       {money(d['daily'])} "
           f"({d['day_fraction'] * 100:.0f}% of the UTC day elapsed)",
           f"  this week:   {money(d['weekly'])}",
           f"  this month:  {money(d['monthly'])}",
           f"  all time:    {money(d['all_time'])}"]

    if d["credits_left"] is not None:
        out.append(f"\nCredits:       {money(d['credits_used'])} used of "
                   f"${d['credits_total']:.2f} ({money(d['credits_left'])} left)")
    if d["limit"]:
        out.append(f"Key cap:       ${d['limit']:.2f} per {d['limit_reset']}, "
                   f"{money(d['limit_remaining'])} remaining")

    out.append("\nProjection (NOT a measurement):")
    if d["no_projection"]:
        out.append(f"  {d['no_projection']}.")
    else:
        out.append(f"  at today's rate: {money(d['per_day'])}/day, {money(d['per_30'])}/30 days")
        out.append(f"  {UPTIME_CAVEAT}")
        out.append(f"  ~{money(d['per_event'])} per scheduled event "
                   f"({PORCH_VISITS_PER_DAY} porch + {SPARKS_PER_DAY} sparks + "
                   f"{POSTS_PER_DAY} post = {EVENTS_PER_DAY}/day)")
        if d["runway_days"] is not None:
            out.append(f"  credit runway: {d['runway_days']:.0f} days")

    out.append("\nA full UTC day of uptime is what makes today's figure trustworthy.")
    return "\n".join(out)


def telegram_report(d):
    """Same numbers, bulleted -- Telegram renders proportional, so the
    terminal report's column alignment would come out ragged."""
    out = [f"💰 {d['model']} spend",
           f"• Today: {money(d['daily'])} ({d['day_fraction'] * 100:.0f}% of the UTC day)",
           f"• This week: {money(d['weekly'])}",
           f"• This month: {money(d['monthly'])}",
           f"• All time: {money(d['all_time'])}"]

    if d["credits_left"] is not None:
        out.append(f"• Credits left: {money(d['credits_left'])} of ${d['credits_total']:.2f}")

    if d["no_projection"]:
        out.append(f"\nNo projection: {d['no_projection']}.")
    else:
        out.append(f"\nAt today's rate: {money(d['per_day'])}/day "
                   f"→ {money(d['per_30'])}/30 days")
        out.append(f"~{money(d['per_event'])} per scheduled event "
                   f"({EVENTS_PER_DAY}/day)")
        if d["runway_days"] is not None:
            out.append(f"Credit runway: {d['runway_days']:.0f} days")
        out.append(f"\n⚠️ {UPTIME_CAVEAT}")
    return "\n".join(out)


def main():
    try:
        print(terminal_report(collect()))
    except CostUnavailable as e:
        print(f"Cost unavailable: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
