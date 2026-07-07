-- CORTEX schema. Append-only logs (captures, archive) + one mutable items table.
PRAGMA journal_mode = WAL;

-- Append-only raw capture log. text is ALREADY anonymized (vault tokens) — real
-- values exist only in the vault table (Golden Rule #4). Rows are never edited
-- or deleted (Golden Rule #6).
CREATE TABLE IF NOT EXISTS captures (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    text             TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'telegram',  -- telegram|api|import
    chat_id          TEXT,
    created_ts       INTEGER NOT NULL,                  -- epoch seconds
    triaged_item_id  INTEGER                            -- set once triage lands; no FK, log stays valid if item deleted
);
CREATE INDEX IF NOT EXISTS idx_captures_ts ON captures(created_ts);

-- The backlog. content is anonymized; tags is a JSON array serialized to TEXT
-- (read/write via db.tags_dumps/tags_loads only).
CREATE TABLE IF NOT EXISTS items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT NOT NULL DEFAULT 'note',    -- task|event|note|idea|question|asset
    content           TEXT NOT NULL,
    priority          TEXT NOT NULL DEFAULT 'normal',  -- low|normal|high
    tags              TEXT NOT NULL DEFAULT '[]',
    due_ts            INTEGER,
    status            TEXT NOT NULL DEFAULT 'open',    -- new|todo|open|tracked|someday|done
    kind              TEXT,                            -- assets only: income|expense|subscription|one-off
    snoozed_until_ts  INTEGER,
    duplicate_of      INTEGER,                         -- warn-only pointer, never auto-merged (Golden Rule #7)
    created_ts        INTEGER NOT NULL,
    updated_ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_due ON items(due_ts);

-- Real values behind TKN- tokens. Readable only via Telegram /reveal
-- (ALLOWED_CHAT_IDS-gated); excluded from CRUD and /api/state.
CREATE TABLE IF NOT EXISTS vault (
    token        TEXT PRIMARY KEY,          -- TKN-XXXXXXXX
    real_value   TEXT NOT NULL,
    currency     TEXT,
    kind         TEXT NOT NULL DEFAULT 'amount',
    record_hint  TEXT,
    created_ts   INTEGER NOT NULL
);

-- Append-only copy of items at the moment they were marked done.
CREATE TABLE IF NOT EXISTS archive (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL,
    type        TEXT NOT NULL,
    content     TEXT NOT NULL,
    priority    TEXT NOT NULL,
    tags        TEXT NOT NULL,
    due_ts      INTEGER,
    status      TEXT NOT NULL,              -- status at done-time (pre-done)
    kind        TEXT,
    created_ts  INTEGER NOT NULL,
    done_ts     INTEGER NOT NULL
);

-- Tiny key/value store for seeded help text + starter tags.
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
