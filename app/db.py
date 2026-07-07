import calendar
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings

RECURRENCES = ("daily", "weekly", "monthly")

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()
_SEED = Path(__file__).with_name("seed.sql").read_text()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(seed: bool = True) -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS doesn't add columns to an already-existing
        # table — guard each new column so re-running stays safe on old DBs.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "recurrence" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN recurrence TEXT")
        if seed:
            # Seed is idempotent (INSERT OR IGNORE).
            conn.executescript(_SEED)


# ---------- tags helpers (items.tags is a JSON array serialized to TEXT) ----------

def _clean_tag(t) -> str:
    """Tags are stored without the leading '#' — that's a display-only convention
    (telegram.format_echo re-adds it). Strip it so manual '#tag' input matches
    what Telegram capture already produces."""
    return str(t).strip().lstrip("#").strip()


def tags_dumps(tags) -> str:
    """Normalize any tags input (list, JSON string, comma string) to JSON text."""
    if tags is None:
        return "[]"
    if isinstance(tags, str):
        s = tags.strip()
        if not s:
            return "[]"
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return json.dumps([_clean_tag(t) for t in parsed if _clean_tag(t)], ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        return json.dumps([_clean_tag(t) for t in s.split(",") if _clean_tag(t)], ensure_ascii=False)
    return json.dumps([_clean_tag(t) for t in tags if _clean_tag(t)], ensure_ascii=False)


def tags_loads(s) -> list[str]:
    if not s:
        return []
    try:
        parsed = json.loads(s)
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


# ---------- item state changes (archive discipline lives here) ----------

def _next_due(due_ts: int, recurrence: str) -> int:
    """Advance a due timestamp by one recurrence interval, in Europe/Prague (or
    settings.tz) wall-clock time so DST shifts don't drift the hour."""
    tz = ZoneInfo(settings.tz)
    dt = datetime.fromtimestamp(due_ts, tz=tz)
    if recurrence == "daily":
        nxt = dt + timedelta(days=1)
    elif recurrence == "weekly":
        nxt = dt + timedelta(days=7)
    else:  # monthly — calendar month, clamp day (e.g. Jan 31 -> Feb 28/29)
        month = dt.month + 1
        year = dt.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        nxt = dt.replace(year=year, month=month, day=day)
    return int(nxt.timestamp())


def mark_done(item_id: int) -> tuple[dict | None, dict | None]:
    """Copy the item to archive (immutable record) and set status=done.

    The item row is kept so search still finds it. If the item has a
    `recurrence`, also inserts the next occurrence (same content/tags/priority,
    due_ts advanced one interval, status back to 'open'). Returns
    (done_item, next_item_or_None); (None, None) if item_id doesn't exist.
    """
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None, None
        item = dict(row)
        next_item = None
        if item["status"] != "done":
            conn.execute(
                """INSERT INTO archive
                   (item_id, type, content, priority, tags, due_ts, status, kind,
                    created_ts, done_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (item["id"], item["type"], item["content"], item["priority"],
                 item["tags"], item["due_ts"], item["status"], item["kind"],
                 item["created_ts"], now),
            )
            conn.execute(
                "UPDATE items SET status = 'done', snoozed_until_ts = NULL, updated_ts = ? WHERE id = ?",
                (now, item_id),
            )
            if item.get("recurrence") in RECURRENCES:
                next_due = _next_due(item["due_ts"] or now, item["recurrence"])
                cur = conn.execute(
                    """INSERT INTO items
                       (type, content, priority, tags, due_ts, status, kind,
                        recurrence, created_ts, updated_ts)
                       VALUES (?,?,?,?,?,'open',?,?,?,?)""",
                    (item["type"], item["content"], item["priority"], item["tags"],
                     next_due, item["kind"], item["recurrence"], now, now),
                )
                next_item = dict(conn.execute(
                    "SELECT * FROM items WHERE id = ?", (cur.lastrowid,)
                ).fetchone())
        item.update(status="done", updated_ts=now)
    return item, next_item


def snooze_item(item_id: int, days: int = 7) -> dict | None:
    now = int(time.time())
    until = now + days * 86400
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE items SET snoozed_until_ts = ?, updated_ts = ? WHERE id = ?",
            (until, now, item_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row)
