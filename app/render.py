"""/backlog.md renderer — curated markdown of open items, Obsidian-ingestable.

Sections by type, one item per line. Deterministic, no I/O beyond the caller's
item list.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .db import tags_loads

_SECTIONS = (
    ("task", "Tasks"),
    ("event", "Events"),
    ("question", "Questions"),
    ("idea", "Ideas"),
    ("note", "Notes"),
    ("asset", "Assets"),
)


def _line(it: dict) -> str:
    parts = [f"- [ ] {it['content']}"]
    if it.get("due_ts"):
        d = datetime.fromtimestamp(it["due_ts"], tz=ZoneInfo(settings.tz))
        parts.append(f"📅 {d.strftime('%Y-%m-%d')}")
    if it.get("priority") == "high":
        parts.append("‼️")
    tags = tags_loads(it.get("tags"))
    if tags:
        parts.append(" ".join(f"#{t}" for t in tags))
    if it.get("status") == "someday":
        parts.append("(someday)")
    parts.append(f"^{it['id']}")
    return " ".join(parts)


def render_backlog(items: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo(settings.tz))
    open_items = [i for i in items if i.get("status") != "done"]
    out = [f"# CORTEX backlog", f"*rendered {now.strftime('%Y-%m-%d %H:%M')} · {len(open_items)} open item(s)*", ""]
    for itype, title in _SECTIONS:
        rows = [i for i in open_items if i.get("type") == itype]
        if not rows:
            continue
        out.append(f"## {title}")
        out += [_line(i) for i in rows]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
