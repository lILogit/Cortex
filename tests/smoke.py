"""Keyless smoke test (CLAUDE.md): no network, no API keys.

Asserts:
  1. anonymizer vaults currency-marked amounts, protects dates/times, is deterministic
  2. regex-fallback triage: type/priority/tags/due resolution (cs + en)
  3. dedup warning against recent items
  4. brief routing: DUE / HIGH / STALE / SOMEDAY / snooze suppression
  5. backlog renderer emits sectioned markdown
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Force a throwaway datastore before any app import (settings reads env at import).
with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _fh:
    _DB = _fh.name
os.environ["DB_PATH"] = _DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.anonymize import anonymize, TOKEN_RE                    # noqa: E402
from app.brief import route                                      # noqa: E402
from app.db import init_db                                       # noqa: E402
from app.llm import _triage_heuristic, resolve_due               # noqa: E402
from app.render import render_backlog                            # noqa: E402

init_db(seed=True)

FAIL = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


TZ = ZoneInfo("Europe/Prague")
TODAY = datetime(2026, 7, 2, 10, 0, tzinfo=TZ)

# ---- 1. anonymizer ----
text = "koupit dalnicni znamku 1500 Kč, expirace 10.07.2026 #car"
anon, entries = anonymize(text)
check("amount vaulted", len(entries) == 1 and "1500" not in anon, f"anon={anon!r}")
check("token format TKN-XXXXXXXX", bool(entries) and TOKEN_RE.fullmatch(entries[0]["token"]),
      str(entries))
check("date survives anonymization", "10.07.2026" in anon, f"anon={anon!r}")
check("currency normalized to CZK", entries and entries[0]["currency"] == "CZK", str(entries))

anon2, entries2 = anonymize(text)
check("anonymization deterministic",
      anon == anon2 and entries[0]["token"] == entries2[0]["token"])

anon3, entries3 = anonymize("schůzka v 17:30, rok 2026, SPZ 1AB 2345")
check("times/years/plates not vaulted", entries3 == [] and "17:30" in anon3, f"{anon3!r} {entries3}")

_, eur = anonymize("předplatné €20 měsíčně")
check("€ prefix amount vaulted as EUR", len(eur) == 1 and eur[0]["currency"] == "EUR", str(eur))

# ---- 2. heuristic triage ----
t = _triage_heuristic("koupit dalnicni znamku zítra !high #car", [], TODAY)
check("actionable verb -> task", t["type"] == "task", str(t))
check("!high -> priority high", t["priority"] == "high", str(t))
check("hashtag extracted", t["tags"] == ["car"], str(t))
expected_zitra = int((TODAY + timedelta(days=1)).replace(hour=9, minute=0).timestamp())
check("zítra -> tomorrow 09:00 Prague", t["due_ts"] == expected_zitra,
      f"due={t['due_ts']} expected={expected_zitra}")
check("content stripped of tag/prio/date words",
      "#" not in t["content"] and "!high" not in t["content"] and "zítra" not in t["content"],
      t["content"])
check("task status todo", t["status"] == "todo", str(t))

e = _triage_heuristic("prohlídka auta, termín 12.8.2026 17:30", [], TODAY)
check("date + deadline word -> event", e["type"] == "event", str(e))
expected_dt = int(datetime(2026, 8, 12, 17, 30, tzinfo=TZ).timestamp())
check("explicit 12.8.2026 17:30 resolved via Europe/Prague", e["due_ts"] == expected_dt,
      f"due={e['due_ts']} expected={expected_dt}")

q = _triage_heuristic("mám prodloužit hosting?", [], TODAY)
check("question mark -> question", q["type"] == "question", str(q))

n = _triage_heuristic("random myšlenka o ničem", [], TODAY)
check("default -> note, status open", n["type"] == "note" and n["status"] == "open", str(n))

due_ts, rest = resolve_due("zaplatit do 10.07.2026", TODAY)
check("resolve_due strips the date words", due_ts is not None and "10.07" not in rest, rest)

# ---- 3. dedup ----
recent = [{"id": 42, "type": "task", "content": "koupit dalnicni znamku", "due_ts": None}]
d = _triage_heuristic("koupit dalnicni znamku na rok", recent, TODAY)
check("dedup warns with duplicate_of", d["duplicate_of"] == 42, str(d))
d2 = _triage_heuristic("uplne jiny text o zahradnim grilu", recent, TODAY)
check("unrelated text -> no duplicate", d2["duplicate_of"] is None, str(d2))

# ---- 4. brief routing ----
now = int(time.time())
base = {"priority": "normal", "status": "open", "snoozed_until_ts": None,
        "due_ts": None, "type": "task", "updated_ts": now}
check("due in 2 days -> DUE", route({**base, "due_ts": now + 2 * 86400}, now) == "DUE")
check("high prio, no due -> HIGH", route({**base, "priority": "high"}, now) == "HIGH")
check("due far away, normal -> unrouted", route({**base, "due_ts": now + 30 * 86400}, now) is None)
check("snoozed DUE item suppressed",
      route({**base, "due_ts": now + 86400, "snoozed_until_ts": now + 86400}, now) is None)
check("stale only in weekly",
      route({**base, "updated_ts": now - 40 * 86400}, now, weekly=False) is None
      and route({**base, "updated_ts": now - 40 * 86400}, now, weekly=True) == "STALE")
check("old idea -> SOMEDAY in weekly",
      route({**base, "type": "idea", "updated_ts": now - 22 * 86400}, now, weekly=True) == "SOMEDAY")
check("done item never routed", route({**base, "status": "done", "due_ts": now}, now) is None)

# ---- 5. renderer ----
md = render_backlog([
    {"id": 1, "type": "task", "content": "koupit znamku", "priority": "high",
     "tags": '["car"]', "due_ts": now + 86400, "status": "todo"},
    {"id": 2, "type": "idea", "content": "cortex export", "priority": "normal",
     "tags": "[]", "due_ts": None, "status": "open"},
    {"id": 3, "type": "task", "content": "hotovo", "priority": "normal",
     "tags": "[]", "due_ts": None, "status": "done"},
])
check("renderer: sections present", "## Tasks" in md and "## Ideas" in md, md)
check("renderer: one line per open item", "koupit znamku" in md and "cortex export" in md, md)
check("renderer: done items excluded", "hotovo" not in md, md)
check("renderer: block ids for Obsidian", "^1" in md, md)

print()
if FAIL:
    print(f"{len(FAIL)} check(s) FAILED: {', '.join(FAIL)}")
    sys.exit(1)
print("ALL CHECKS PASSED")
