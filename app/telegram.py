"""Telegram I/O: send briefs/echoes, parse webhook commands + callback buttons.

Plain-text captures are handled in main.py (capture insert must happen FIRST and
return 200 before triage — rule 2); this module owns everything that starts with
"/" plus the inline-button callbacks.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import settings
from .db import get_conn, mark_done, snooze_item, tags_loads

API = "https://api.telegram.org/bot{token}/{method}"


async def _call(method: str, payload: dict) -> dict:
    if not settings.telegram_bot_token:
        return {}
    url = API.format(token=settings.telegram_bot_token, method=method)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        return r.json()


async def send_message(text: str, keyboard: list[list[dict]] | None = None,
                       chat_id: str | None = None) -> dict:
    payload: dict = {"chat_id": chat_id or settings.telegram_chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return await _call("sendMessage", payload)


def allowed(chat_id: str) -> bool:
    """ALLOWED_CHAT_IDS gates capture and /reveal; empty = allow all (dev)."""
    ids = settings.allowed_chat_id_set()
    return not ids or str(chat_id) in ids


# ---------- echo (Golden Rule #5: echo the applied diff) ----------

def format_echo(item: dict, dup: dict | None = None) -> str:
    tz = ZoneInfo(settings.tz)
    lines = [f"📥 {item['type']} #{item['id']} — {item['content']}"]
    detail = []
    if item.get("due_ts"):
        detail.append("due " + datetime.fromtimestamp(item["due_ts"], tz=tz).strftime("%d.%m.%Y %H:%M"))
    if item.get("priority") and item["priority"] != "normal":
        detail.append(f"prio {item['priority']}")
    tags = tags_loads(item.get("tags"))
    if tags:
        detail.append(" ".join(f"#{t}" for t in tags))
    if item.get("kind"):
        detail.append(f"kind {item['kind']}")
    if detail:
        lines.append(" · ".join(detail))
    if dup:
        warn = f"⚠️ possible duplicate of #{dup['id']}: {dup['content']}"
        if dup.get("due_ts") and item.get("due_ts") and dup["due_ts"] != item["due_ts"]:
            d = datetime.fromtimestamp(dup["due_ts"], tz=tz).strftime("%d.%m.%Y")
            n = datetime.fromtimestamp(item["due_ts"], tz=tz).strftime("%d.%m.%Y")
            warn += f" (date conflict: new says {n}, existing says {d})"
        lines.append(warn)
    return "\n".join(lines)


# ---------- inbound webhook (commands + callbacks) ----------

async def handle_update(update: dict) -> None:
    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
        return
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if text.startswith("/"):
        await _handle_command(text, chat_id)


async def _handle_callback(cq: dict) -> None:
    data = cq.get("data", "")
    if ":" not in data:
        return
    action, item_id_s = data.split(":", 1)
    try:
        item_id = int(item_id_s)
    except ValueError:
        return
    await _call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": f"{action} ✓"})
    if action == "done":
        item = mark_done(item_id)
        await send_message(f"✅ done #{item_id} — {item['content']}" if item
                           else f"Item #{item_id} not found.")
    elif action == "snooze":
        item = snooze_item(item_id, 7)
        await send_message(f"💤 snoozed #{item_id} for 7 days — {item['content']}" if item
                           else f"Item #{item_id} not found.")
    elif action == "someday":
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE items SET status = 'someday', updated_ts = ? WHERE id = ?",
                (int(time.time()), item_id),
            )
        await send_message(f"🗂 moved #{item_id} to someday." if cur.rowcount
                           else f"Item #{item_id} not found.")


async def _handle_command(text: str, chat_id: str) -> None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'help_text'").fetchone()
        await send_message(row["value"] if row else "No help text seeded.", chat_id=chat_id)

    elif cmd == "/brief":
        from .brief import build_and_send_brief  # late import to avoid cycle
        await build_and_send_brief("daily")

    elif cmd == "/week":
        from .brief import build_and_send_brief
        await build_and_send_brief("weekly")

    elif cmd == "/done":
        item_id = _int_or_none(arg.split()[0] if arg else "")
        if item_id is None:
            await send_message("Usage: /done <id>", chat_id=chat_id)
            return
        item = mark_done(item_id)
        await send_message(f"✅ done #{item_id} — {item['content']}" if item
                           else f"Item #{item_id} not found.", chat_id=chat_id)

    elif cmd == "/snooze":
        args = arg.split()
        item_id = _int_or_none(args[0]) if args else None
        days = _int_or_none(args[1]) if len(args) > 1 else 7
        if item_id is None or days is None:
            await send_message("Usage: /snooze <id> [days]", chat_id=chat_id)
            return
        item = snooze_item(item_id, days)
        await send_message(f"💤 snoozed #{item_id} for {days} days — {item['content']}" if item
                           else f"Item #{item_id} not found.", chat_id=chat_id)

    elif cmd == "/reveal":
        if not allowed(chat_id):
            await send_message("Not allowed.", chat_id=chat_id)
            return
        token = arg.split()[0].upper() if arg else ""
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM vault WHERE token = ?", (token,)).fetchone()
        if not row:
            await send_message(f"No vault entry for {token or '(missing token)'}.", chat_id=chat_id)
            return
        v = dict(row)
        hint = f"\ncontext: …{v['record_hint']}" if v.get("record_hint") else ""
        await send_message(
            f"🔓 {v['token']} = {v['real_value']} {v.get('currency') or ''}".rstrip() + hint,
            chat_id=chat_id,
        )

    elif cmd == "/??":
        if not arg:
            await send_message("Usage: /?? <query>", chat_id=chat_id)
            return
        like = f"%{arg}%"
        with get_conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM items WHERE content LIKE ? OR tags LIKE ? "
                "ORDER BY updated_ts DESC LIMIT 10", (like, like),
            ).fetchall()]
        if not rows:
            await send_message(f"No items match “{arg}”.", chat_id=chat_id)
            return
        lines = [f"#{r['id']} [{r['type']}/{r['status']}] {r['content']}" for r in rows]
        await send_message("🔎 " + arg + "\n" + "\n".join(lines), chat_id=chat_id)

    else:
        await send_message("Unknown command. /help lists what I understand.", chat_id=chat_id)


def _int_or_none(s: str):
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
