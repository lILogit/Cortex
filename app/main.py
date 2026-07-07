"""CORTEX — single-process FastAPI service.

Endpoints:
  POST /tg                   Telegram webhook — captures, commands, callback buttons.
  GET  /dashboard            Web dashboard.
  GET  /tables               Generic CRUD editor UI for items.
  GET  /api/state            Dashboard data (JSON).
  GET  /backlog.md           Curated markdown render of open items.
  POST /api/capture          Programmatic capture (same anonymize→triage path).
  CRUD /api/items[/{id}]     Generic CRUD over items.
  POST /api/import/n8n-csv   One-off migration of Cortex v2 Data Tables exports.
  GET  /admin, GET /health
"""
import csv as _csv
import io
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .anonymize import anonymize
from . import auth
from .config import settings
from .db import get_conn, init_db, mark_done, tags_dumps, tags_loads
from .jobs import build_scheduler
from .llm import triage
from .render import render_backlog
from . import telegram

scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(seed=True)
    global scheduler
    scheduler = build_scheduler()
    scheduler.start()
    # Register Telegram webhook if we know our public URL.
    if settings.telegram_bot_token and settings.public_base_url:
        await telegram._call(
            "setWebhook", {"url": f"{settings.public_base_url.rstrip('/')}/tg"}
        )
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="CORTEX", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=auth.get_session_secret())


def _parse_ts(s: str | None) -> int:
    """Parse inbound timestamps once, here. Everything downstream is epoch seconds."""
    if not s:
        return int(time.time())
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return int(time.time())


# ---------------------------- capture pipeline (Loop B) ----------------------------

def insert_capture(text: str, source: str, chat_id: str | None = None,
                   created_ts: int | None = None) -> tuple[int, str]:
    """Anonymize (pure, can't lose data — falls back to raw on any error) and
    append the capture + vault rows in one transaction. Returns (id, anon_text)."""
    try:
        anon, entries = anonymize(text)
    except Exception:
        anon, entries = text, []
    now = int(time.time())
    with get_conn() as conn:
        for e in entries:
            conn.execute(
                "INSERT OR IGNORE INTO vault (token, real_value, currency, kind, record_hint, created_ts) "
                "VALUES (?,?,?,?,?,?)",
                (e["token"], e["real_value"], e["currency"], e["kind"], e["record_hint"], now),
            )
        cur = conn.execute(
            "INSERT INTO captures (text, source, chat_id, created_ts) VALUES (?,?,?,?)",
            (anon, source, chat_id, created_ts or now),
        )
    return cur.lastrowid, anon


def _recent_open_items() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE status != 'done' ORDER BY id DESC LIMIT ?",
            (settings.recent_items_for_dedup,),
        ).fetchall()]


def triage_capture(capture_id: int, anon_text: str) -> tuple[dict, dict | None]:
    """Triage an already-stored capture into an item row. Returns (item, dup_item)."""
    recent = _recent_open_items()
    t = triage(anon_text, recent)
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO items
               (type, content, priority, tags, due_ts, status, kind,
                duplicate_of, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (t["type"], t["content"], t["priority"], tags_dumps(t["tags"]),
             t["due_ts"], t["status"], t["kind"], t["duplicate_of"], now, now),
        )
        item_id = cur.lastrowid
        conn.execute(
            "UPDATE captures SET triaged_item_id = ? WHERE id = ?", (item_id, capture_id)
        )
        item = dict(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
        dup = None
        if t["duplicate_of"]:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (t["duplicate_of"],)).fetchone()
            dup = dict(row) if row else None
    return item, dup


async def _ack_triage_and_echo(capture_id: int, anon_text: str, chat_id: str) -> None:
    """Background task behind POST /tg — the webhook already returned 200.

    Sends an instant ack to the sender's chat first (triage can take a moment,
    especially on the LLM path), then the processed-format echo once done.
    """
    await telegram.send_message("📥 got it — processing…", chat_id=chat_id)
    item, dup = triage_capture(capture_id, anon_text)
    await telegram.send_message(telegram.format_echo(item, dup))


# ------------------------------ Telegram webhook ------------------------------

@app.post("/tg")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    update = await request.json()
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))

    if "callback_query" in update or text.startswith("/"):
        await telegram.handle_update(update)
        return {"ok": True}

    if text:
        if not telegram.allowed(chat_id):
            return {"ok": True}
        # Rule 2: capture lands append-only FIRST; triage runs after we return 200.
        capture_id, anon = insert_capture(text, "telegram", chat_id,
                                          _parse_ts(str(msg.get("date", ""))))
        background_tasks.add_task(_ack_triage_and_echo, capture_id, anon, chat_id)
    return {"ok": True}


# ------------------------------ programmatic capture (Loop C entry) ------------------------------

@app.post("/api/capture")
async def api_capture(request: Request):
    """Same anonymize→triage path as Telegram text; runs triage inline so the
    caller gets the resulting item back."""
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "text required")
    source = body.get("source", "api")
    if source not in ("telegram", "api", "import"):
        source = "api"
    capture_id, anon = insert_capture(text, source)
    item, dup = triage_capture(capture_id, anon)
    await telegram.send_message(telegram.format_echo(item, dup))
    item["tags"] = tags_loads(item["tags"])
    return {"capture_id": capture_id, "item": item,
            "duplicate_warning": telegram.format_echo(dup) if dup else None}


# ------------------------------ dashboard / export ------------------------------

@app.get("/login")
async def login_form():
    return HTMLResponse(auth.render_login_html())


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if auth.check_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse("/dashboard", status_code=303)
    return HTMLResponse(auth.render_login_html("Invalid username or password"), status_code=401)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard")
async def dashboard(_: None = Depends(auth.require_login)):
    return HTMLResponse(Path(__file__).with_name("dashboard.html").read_text())


@app.get("/tables")
async def tables_page(_: None = Depends(auth.require_login)):
    return HTMLResponse(Path(__file__).with_name("tables.html").read_text())


@app.get("/api/state")
async def api_state():
    from .brief import build_brief
    b = build_brief(weekly=True)
    with get_conn() as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM items ORDER BY status = 'done', updated_ts DESC LIMIT 100"
        ).fetchall()]
        captures = [dict(r) for r in conn.execute(
            "SELECT id, text, source, created_ts, triaged_item_id FROM captures "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()]
        counts = {
            "captures": conn.execute("SELECT COUNT(*) n FROM captures").fetchone()["n"],
            "items_open": conn.execute(
                "SELECT COUNT(*) n FROM items WHERE status NOT IN ('done','someday')").fetchone()["n"],
            "items_someday": conn.execute(
                "SELECT COUNT(*) n FROM items WHERE status = 'someday'").fetchone()["n"],
            "items_done": conn.execute(
                "SELECT COUNT(*) n FROM items WHERE status = 'done'").fetchone()["n"],
            "archive": conn.execute("SELECT COUNT(*) n FROM archive").fetchone()["n"],
            # count only — real vault values never leave /reveal (rule 4)
            "vault": conn.execute("SELECT COUNT(*) n FROM vault").fetchone()["n"],
        }
    for it in items:
        it["tags"] = tags_loads(it["tags"])
    for section in ("due", "high", "stale", "someday_candidates"):
        for it in b[section]:
            it["tags"] = tags_loads(it["tags"])
    return {
        "counts": counts,
        "due": b["due"],
        "high": b["high"],
        "stale": b["stale"],
        "someday_candidates": b["someday_candidates"],
        "tag_counts": b["tag_counts"],
        "items": items,
        "captures": captures,
    }


@app.get("/backlog.md")
async def backlog_md():
    with get_conn() as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE status != 'done' "
            "ORDER BY due_ts IS NULL, due_ts ASC, id DESC"
        ).fetchall()]
    return PlainTextResponse(render_backlog(items), media_type="text/markdown")


# ------------------------------ admin ------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin")
async def admin():
    with get_conn() as conn:
        captures = conn.execute("SELECT COUNT(*) n FROM captures").fetchone()["n"]
        items = [dict(r) for r in conn.execute(
            "SELECT id, type, content, priority, status, due_ts FROM items "
            "ORDER BY id DESC LIMIT 50").fetchall()]
        open_n = conn.execute(
            "SELECT COUNT(*) n FROM items WHERE status NOT IN ('done','someday')").fetchone()["n"]
    return {"captures": captures, "items_open": open_n, "items": items}


# ------------------------------ generic CRUD over items ------------------------------
# captures/vault/archive are intentionally NOT exposed: captures/archive are
# pipeline-owned append-only logs, and the vault holds secrets (Telegram
# /reveal only).

_ITEM_COLUMNS = {
    "type", "content", "priority", "tags", "due_ts", "status", "kind",
    "snoozed_until_ts", "duplicate_of",
}
_ITEM_ENUMS = {
    "type": {"task", "event", "note", "idea", "question", "asset"},
    "priority": {"low", "normal", "high"},
    "status": {"new", "todo", "open", "tracked", "someday", "done"},
    "kind": {"income", "expense", "subscription", "one-off"},
}


def _validate_item_fields(fields: dict) -> dict:
    for field, allowed in _ITEM_ENUMS.items():
        if field in fields and fields[field] is not None and fields[field] not in allowed:
            raise HTTPException(400, f"{field} must be one of {sorted(allowed)}")
    if "tags" in fields:
        fields["tags"] = tags_dumps(fields["tags"])
    return fields


def _item_out(row) -> dict:
    d = dict(row)
    d["tags"] = tags_loads(d["tags"])
    return d


@app.get("/api/items")
async def items_list():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
    return [_item_out(r) for r in rows]


@app.get("/api/items/{item_id}")
async def items_get(item_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"item {item_id} not found")
    return _item_out(row)


@app.post("/api/items")
async def items_create(request: Request):
    body = await request.json()
    missing = {"type", "content"} - body.keys()
    if missing:
        raise HTTPException(400, f"missing required fields: {sorted(missing)}")
    fields = _validate_item_fields({k: v for k, v in body.items() if k in _ITEM_COLUMNS})
    now = int(time.time())
    fields.setdefault("tags", "[]")
    fields["created_ts"] = now
    fields["updated_ts"] = now
    cols = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                f"INSERT INTO items ({cols}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            row = conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, str(e))
    return _item_out(row)


@app.put("/api/items/{item_id}")
async def items_update(item_id: int, request: Request):
    body = await request.json()
    fields = _validate_item_fields({k: v for k, v in body.items() if k in _ITEM_COLUMNS})
    if not fields:
        raise HTTPException(400, f"nothing to update; allowed fields: {sorted(_ITEM_COLUMNS)}")
    # done goes through mark_done so the archive copy is never skipped (rule 6)
    if fields.get("status") == "done":
        if mark_done(item_id) is None:
            raise HTTPException(404, f"item {item_id} not found")
        fields.pop("status")
        if not fields:
            with get_conn() as conn:
                row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            return _item_out(row)
    fields["updated_ts"] = int(time.time())
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE items SET {set_clause} WHERE id = ?",
            (*fields.values(), item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"item {item_id} not found")
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return _item_out(row)


@app.delete("/api/items/{item_id}")
async def items_delete(item_id: int):
    with get_conn() as conn:
        # captures.triaged_item_id has no FK on purpose — the append-only log
        # keeps pointing at the id even after the item is gone.
        cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"item {item_id} not found")
    return {"deleted": item_id}


# ------------------------------ n8n Data Tables migration ------------------------------

def _pick(row: dict, *names: str) -> str:
    """Case-insensitive column lookup across the export's naming variants."""
    lower = {k.lower().strip(): v for k, v in row.items() if k}
    for n in names:
        v = lower.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


@app.post("/api/import/n8n-csv")
async def import_n8n_csv(inbox: UploadFile | None = File(None),
                         vault: UploadFile | None = File(None)):
    """One-off idempotent migration of Cortex v2 Data Tables CSV exports.

    Inbox rows → captures + items (heuristic triage — no API cost for bulk);
    Vault rows → vault (INSERT OR IGNORE on token). A row is skipped when a
    capture with the same text + created_ts already exists.
    """
    from .llm import _triage_heuristic

    result = {"captures_imported": 0, "items_created": 0,
              "vault_imported": 0, "skipped": 0}

    if vault is not None:
        reader = _csv.DictReader(io.StringIO((await vault.read()).decode("utf-8-sig")))
        now = int(time.time())
        with get_conn() as conn:
            for row in reader:
                token = _pick(row, "token")
                value = _pick(row, "real_value", "value")
                if not token or not value:
                    result["skipped"] += 1
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO vault (token, real_value, currency, kind, record_hint, created_ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (token.upper(), value, _pick(row, "currency") or None,
                     _pick(row, "kind") or "amount", _pick(row, "record_hint", "hint") or None,
                     _parse_ts(_pick(row, "createdat", "created_ts", "created")) or now),
                )
                result["vault_imported"] += cur.rowcount

    if inbox is not None:
        reader = _csv.DictReader(io.StringIO((await inbox.read()).decode("utf-8-sig")))
        for row in reader:
            text = _pick(row, "text", "content", "message", "raw")
            if not text:
                result["skipped"] += 1
                continue
            created_ts = _parse_ts(_pick(row, "createdat", "created_ts", "created", "timestamp"))
            with get_conn() as conn:
                dupe = conn.execute(
                    "SELECT id FROM captures WHERE text = ? AND created_ts = ?",
                    (text, created_ts),
                ).fetchone()
            if dupe:
                result["skipped"] += 1
                continue
            capture_id, anon = insert_capture(text, "import", created_ts=created_ts)
            t = _triage_heuristic(anon, [])
            # Columns present in the export win over the heuristic guess.
            itype = _pick(row, "type").lower()
            if itype in _ITEM_ENUMS["type"]:
                t["type"] = itype
            status = _pick(row, "status").lower()
            if status in _ITEM_ENUMS["status"]:
                t["status"] = status
            priority = _pick(row, "priority").lower()
            if priority in _ITEM_ENUMS["priority"]:
                t["priority"] = priority
            with get_conn() as conn:
                cur = conn.execute(
                    """INSERT INTO items
                       (type, content, priority, tags, due_ts, status, kind,
                        duplicate_of, created_ts, updated_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (t["type"], t["content"], t["priority"], tags_dumps(t["tags"]),
                     t["due_ts"], t["status"], t["kind"], None, created_ts, created_ts),
                )
                conn.execute("UPDATE captures SET triaged_item_id = ? WHERE id = ?",
                             (cur.lastrowid, capture_id))
            result["captures_imported"] += 1
            result["items_created"] += 1

    return result
