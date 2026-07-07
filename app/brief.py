"""Loop A — build and send the daily brief / weekly review.

Routing: DUE (due_ts ≤ now+7d, not snoozed) · HIGH (priority high, no near due) ·
STALE (updated_ts older than STALE_DAYS, weekly only) · SOMEDAY (ideas untouched
across 3 weekly reviews — proposed, never auto-moved).
"""
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .db import get_conn, tags_loads

OPEN_STATUSES = ("new", "todo", "open", "tracked")
_SOMEDAY_UNTOUCHED_S = 3 * 7 * 86400  # 3 weekly reviews


def route(item: dict, now: int | None = None, weekly: bool = False) -> str | None:
    """Pure routing decision for one open item."""
    now = now or int(time.time())
    if item["status"] not in OPEN_STATUSES:
        return None
    if item.get("snoozed_until_ts") and item["snoozed_until_ts"] > now:
        return None
    if item.get("due_ts") and item["due_ts"] <= now + 7 * 86400:
        return "DUE"
    if item.get("priority") == "high":
        return "HIGH"
    if weekly:
        if (item.get("type") == "idea"
                and now - item["updated_ts"] >= _SOMEDAY_UNTOUCHED_S):
            return "SOMEDAY"
        if now - item["updated_ts"] >= settings.stale_days * 86400:
            return "STALE"
    return None


def build_brief(weekly: bool = False, now: int | None = None) -> dict:
    """Route every open item into sections; weekly adds STALE/SOMEDAY/tag counts."""
    now = now or int(time.time())
    with get_conn() as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE status NOT IN ('done', 'someday') ORDER BY due_ts IS NULL, due_ts ASC, id DESC"
        ).fetchall()]
        done_last_week = conn.execute(
            "SELECT COUNT(*) n FROM archive WHERE done_ts >= ?", (now - 7 * 86400,)
        ).fetchone()["n"]

    sections: dict[str, list[dict]] = {"DUE": [], "HIGH": [], "STALE": [], "SOMEDAY": []}
    for it in items:
        r = route(it, now, weekly)
        if r:
            sections[r].append(it)

    tag_counts = Counter(t for it in items for t in tags_loads(it["tags"]))
    return {
        "now": now,
        "weekly": weekly,
        "due": sections["DUE"],
        "high": sections["HIGH"],
        "stale": sections["STALE"] if weekly else [],
        "someday_candidates": sections["SOMEDAY"] if weekly else [],
        "tag_counts": dict(tag_counts.most_common()),
        "open_count": len(items),
        "done_last_week": done_last_week,
    }


def _fmt_due(due_ts: int | None) -> str:
    if not due_ts:
        return ""
    dt = datetime.fromtimestamp(due_ts, tz=ZoneInfo(settings.tz))
    return dt.strftime(" · due %d.%m.%Y %H:%M") if (dt.hour, dt.minute) != (9, 0) \
        else dt.strftime(" · due %d.%m.%Y")


def _fmt_item(it: dict) -> str:
    tags = tags_loads(it["tags"])
    tag_str = " " + " ".join(f"#{t}" for t in tags) if tags else ""
    prio = " ‼️" if it["priority"] == "high" else ""
    return f"#{it['id']} [{it['type']}] {it['content']}{_fmt_due(it['due_ts'])}{prio}{tag_str}"


async def build_and_send_brief(kind: str = "daily") -> dict:
    """Build the brief and push it to Telegram with per-item ✅/💤 buttons."""
    from . import telegram  # late import: telegram command handlers import this module

    weekly = kind == "weekly"
    b = build_brief(weekly=weekly)

    lines = [f"🧠 CORTEX {'weekly review' if weekly else 'daily brief'}"]
    if b["due"]:
        lines.append("\n📅 Due (≤ 7 days):")
        lines += [f"  {_fmt_item(it)}" for it in b["due"]]
    if b["high"]:
        lines.append("\n‼️ High priority:")
        lines += [f"  {_fmt_item(it)}" for it in b["high"]]
    if weekly and b["stale"]:
        lines.append(f"\n🕸 Stale (> {settings.stale_days}d untouched):")
        lines += [f"  {_fmt_item(it)}" for it in b["stale"]]
    if weekly and b["someday_candidates"]:
        lines.append("\n🗂 Someday candidates (confirm below):")
        lines += [f"  {_fmt_item(it)}" for it in b["someday_candidates"]]
    if weekly and b["tag_counts"]:
        lines.append("\n🏷 " + " · ".join(f"#{t} {n}" for t, n in list(b["tag_counts"].items())[:10]))
    if not (b["due"] or b["high"] or (weekly and (b["stale"] or b["someday_candidates"]))):
        lines.append("\nNothing due or urgent. ✨")
    if weekly:
        from .llm import weekly_summary
        with get_conn() as conn:
            open_items = [dict(r) for r in conn.execute(
                "SELECT * FROM items WHERE status NOT IN ('done','someday')").fetchall()]
        lines.append("\n" + weekly_summary(open_items, b["done_last_week"]))

    keyboard = []
    for it in (b["due"] + b["high"])[:8]:
        keyboard.append([
            {"text": f"✅ Done #{it['id']}", "callback_data": f"done:{it['id']}"},
            {"text": f"💤 Snooze #{it['id']}", "callback_data": f"snooze:{it['id']}"},
        ])
    for it in b["someday_candidates"][:4]:
        keyboard.append([
            {"text": f"🗂 Someday #{it['id']}", "callback_data": f"someday:{it['id']}"},
        ])

    await telegram.send_message("\n".join(lines), keyboard or None)
    return b
