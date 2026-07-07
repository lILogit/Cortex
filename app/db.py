import json
import sqlite3
import time
from pathlib import Path

from .config import settings

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
        if seed:
            # Seed is idempotent (INSERT OR IGNORE).
            conn.executescript(_SEED)


# ---------- tags helpers (items.tags is a JSON array serialized to TEXT) ----------

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
                return json.dumps([str(t) for t in parsed], ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        return json.dumps([t.strip() for t in s.split(",") if t.strip()], ensure_ascii=False)
    return json.dumps([str(t) for t in tags], ensure_ascii=False)


def tags_loads(s) -> list[str]:
    if not s:
        return []
    try:
        parsed = json.loads(s)
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


# ---------- item state changes (archive discipline lives here) ----------

def mark_done(item_id: int) -> dict | None:
    """Copy the item to archive (immutable record) and set status=done.

    The item row is kept so search still finds it. Returns the item or None.
    """
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
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
        item.update(status="done", updated_ts=now)
    return item


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
