"""APScheduler jobs — loop A (briefs), the hourly due-scan, loop C stub."""
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .db import get_conn
from .brief import build_and_send_brief, _fmt_item
from . import telegram


async def morning_brief() -> None:
    await build_and_send_brief("daily")


async def weekly_review() -> None:
    await build_and_send_brief("weekly")


async def due_scan() -> None:
    """Hourly: un-snooze items whose snooze expired and re-surface them."""
    now = int(time.time())
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE snoozed_until_ts IS NOT NULL "
            "AND snoozed_until_ts <= ? AND status NOT IN ('done','someday')",
            (now,),
        ).fetchall()]
        for r in rows:
            conn.execute(
                "UPDATE items SET snoozed_until_ts = NULL, updated_ts = ? WHERE id = ?",
                (now, r["id"]),
            )
    if rows:
        lines = ["⏰ Back from snooze:"] + [f"  {_fmt_item(r)}" for r in rows]
        keyboard = [
            [{"text": f"✅ Done #{r['id']}", "callback_data": f"done:{r['id']}"},
             {"text": f"💤 Snooze #{r['id']}", "callback_data": f"snooze:{r['id']}"}]
            for r in rows[:8]
        ]
        await telegram.send_message("\n".join(lines), keyboard)


async def ingest() -> None:
    """Loop C stub: fetch email/calendar/RSS and feed POST /api/capture's path.

    The manual endpoint works; automated source fetch is deliberate open work.
    """
    return None


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.tz)
    sched.add_job(morning_brief, CronTrigger(hour=settings.brief_hour, minute=0),
                  id="morning_brief")
    sched.add_job(weekly_review,
                  CronTrigger(day_of_week=settings.weekly_review_day,
                              hour=settings.weekly_review_hour),
                  id="weekly_review")
    sched.add_job(due_scan, CronTrigger(minute=0), id="due_scan")  # hourly
    sched.add_job(ingest, CronTrigger(hour=6, minute=0), id="ingest")  # loop C stub
    return sched
