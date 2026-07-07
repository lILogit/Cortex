"""Live end-to-end probe against the running uvicorn server (keyless).

Walks the loops over real HTTP:
  POST /api/capture           -> capture row + triaged item + vault round-trip
  POST /api/capture (similar) -> dedup warning surfaced, never merged
  POST /tg plain text         -> 200 first, background triage lands the item
  /tg callback done:{id}      -> item done + archive copy
  brief build (in-process)    -> routed sections contain the due item
  GET /backlog.md, /admin     -> render + counts
"""
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("CORTEX_BASE", "http://127.0.0.1:8000")
fails = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ---------- DB helpers (same file the server uses) ----------
from app.db import get_conn  # noqa: E402


def counts():
    with get_conn() as c:
        return {
            "captures": c.execute("SELECT COUNT(*) n FROM captures").fetchone()["n"],
            "items": c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"],
            "vault": c.execute("SELECT COUNT(*) n FROM vault").fetchone()["n"],
            "archive": c.execute("SELECT COUNT(*) n FROM archive").fetchone()["n"],
        }


print("== 1. /api/capture: anonymize -> triage -> item ==")
before = counts()
r = httpx.post(f"{BASE}/api/capture", json={
    "text": "koupit dalnicni znamku 1500 Kč, expirace 10.07.2026 important #car",
    "source": "api",
}, timeout=30)
check("capture returns 200", r.status_code == 200, r.text)
d = r.json()
item = d["item"]
check("capture row written", counts()["captures"] == before["captures"] + 1)
check("item created", item["id"] > 0, item)
check("priority high (important)", item["priority"] == "high", item)
check("tag car extracted", "car" in item["tags"], item)
check("amount replaced by TKN token", "TKN-" in item["content"] and "1500" not in item["content"],
      item["content"])
check("due resolved (10.07.2026)", bool(item["due_ts"]), item)

print("\n== 2. vault round-trip ==")
token = next(w for w in item["content"].split() if w.startswith("TKN-")).strip(",.")
with get_conn() as conn:
    v = conn.execute("SELECT * FROM vault WHERE token = ?", (token,)).fetchone()
check("vault row exists for token", v is not None, token)
check("vault keeps the real value", v and v["real_value"].startswith("1500"), dict(v) if v else None)
with get_conn() as conn:
    leaked = conn.execute(
        "SELECT COUNT(*) n FROM items WHERE content LIKE '%1500%'").fetchone()["n"]
check("real value leaked nowhere in items", leaked == 0)

print("\n== 3. dedup warning (never merges) ==")
r2 = httpx.post(f"{BASE}/api/capture", json={
    "text": "koupit dalnicni znamku 1500 Kč #car", "source": "api"}, timeout=30)
d2 = r2.json()
check("second similar capture -> duplicate_of set",
      d2["item"]["duplicate_of"] == item["id"], d2["item"])
check("dedup did NOT merge (new row exists)", d2["item"]["id"] != item["id"])

print("\n== 4. /tg plain text: capture first, triage in background ==")
before = counts()
r3 = httpx.post(f"{BASE}/tg", json={
    "message": {"text": "zavolat doktorovi zítra #health",
                "chat": {"id": 1}, "from": {"id": 1}, "date": int(time.time())},
}, timeout=15)
check("/tg returns ok immediately", r3.json().get("ok") is True, r3.text)
for _ in range(20):  # background task; poll briefly
    if counts()["items"] > before["items"]:
        break
    time.sleep(0.25)
c = counts()
check("capture appended", c["captures"] == before["captures"] + 1, c)
check("background triage created item", c["items"] == before["items"] + 1, c)
with get_conn() as conn:
    tg_item = dict(conn.execute("SELECT * FROM items ORDER BY id DESC LIMIT 1").fetchone())
    cap = dict(conn.execute("SELECT * FROM captures ORDER BY id DESC LIMIT 1").fetchone())
check("tg item is a task with due", tg_item["type"] == "task" and tg_item["due_ts"], tg_item)
check("capture linked to item", cap["triaged_item_id"] == tg_item["id"], cap)

print("\n== 5. done via /tg callback -> archive copy ==")
httpx.post(f"{BASE}/tg", json={
    "callback_query": {"id": "x", "data": f"done:{tg_item['id']}", "from": {"id": 1}},
}, timeout=15)
with get_conn() as conn:
    st = conn.execute("SELECT status FROM items WHERE id = ?", (tg_item["id"],)).fetchone()["status"]
    arch = conn.execute("SELECT COUNT(*) n FROM archive WHERE item_id = ?", (tg_item["id"],)).fetchone()["n"]
check("item marked done", st == "done", st)
check("archive row written", arch == 1)

print("\n== 6. brief build routes items ==")
# An item due tomorrow is deterministically inside the 7-day DUE window
# (the 10.07.2026 item may fall outside it depending on the run date).
r4 = httpx.post(f"{BASE}/api/capture", json={
    "text": "prohlídka auta, termín zítra", "source": "api"}, timeout=30)
due_item = r4.json()["item"]
from app.brief import build_brief  # noqa: E402
b = build_brief()
due_ids = [i["id"] for i in b["due"]]
high_ids = [i["id"] for i in b["high"]]
check("item due tomorrow routed to DUE", due_item["id"] in due_ids, due_ids)
check("high-priority item surfaces in DUE or HIGH",
      item["id"] in due_ids + high_ids, {"due": due_ids, "high": high_ids})

print("\n== 7. /backlog.md + /admin ==")
md = httpx.get(f"{BASE}/backlog.md", timeout=15).text
check("backlog.md renders open items", "TKN-" in md and "## " in md)
check("backlog.md excludes done items", tg_item["content"] not in md)
adm = httpx.get(f"{BASE}/admin", timeout=15).json()
check("/admin counts", adm["captures"] >= 3 and adm["items_open"] >= 1, adm)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("ALL E2E CHECKS PASSED")
