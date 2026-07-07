# CORTEX — Personal Backlog & Capture System

One FastAPI process, one SQLite file. Telegram text in → anonymize → LLM triage →
item row → daily brief → human ✅/💤 → archive. Runs fully without API keys
(regex-fallback triage).

See [CLAUDE.md](CLAUDE.md) for architecture, golden rules, and the full command
reference.

## Quick start

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.test
CORTEX_ENV_FILE=.env.test uvicorn app.main:app --reload --port 8000
# Dashboard: http://localhost:8000/dashboard
```

Tests:

```sh
python3 tests/smoke.py       # keyless, no network
python3 tests/e2e_probe.py   # needs the dev server on :8000
```

## Message format catalog

Plain text sent to the bot is triaged into one of six types. There's no
required syntax — triage reads the wording — but phrasing a capture with these
markers gets a reliably correct result in both LLM mode and the keyless
regex-fallback mode (same trigger words in both).

| Type | Triggers on | Example |
|---|---|---|
| **task** | an actionable verb — koupit, dokončit, zavolat, ověřit, zaplatit, poslat, objednat, zařídit / buy, call, finish, pay, send, check, fix, book | `zavolat doktorovi kvůli terminu !high #health` |
| **event** (schedule) | a resolved due date, especially paired with schůzka, termín, deadline, expir\*, expiry, appointment, meeting, prohlídka — *any date with no task verb also defaults to event* | `STK prohlídka 12.8.2026 17:00 #car` |
| **note** | the catch-all — no verb, no date, no `?`, no idea/asset words. Forwarded notifications land here unless they carry a deadline (then they're an event) | `Kolega zmínil problém s API rate limitem` |
| **idea** | nápad, idea, co kdyby / what if | `idea: export do Obsidian přes git push #cortex` |
| **question** | text ends with `?` | `Kolik stojí roční predplatne Todoist?` |
| **asset** | předplatné/subscription, faktura/invoice, výplata/platba/payment — `kind` sub-classifies as subscription / income / expense (expense is the default) | `Spotify predplatne 149 Kč mesicne #subscription` |

**Modifiers, usable on any type:**

- **Tags** — `#word` (as many as you like, lowercase, stripped from the stored content).
- **Priority** — `!high`, or urgent/naléhavě/důležité/important anywhere in the
  text ⇒ `priority: high`. No explicit low marker; anything else is `normal`.
- **Due date** — explicit `dd.mm.yyyy [hh:mm]` wins over relative words;
  otherwise dnes/today (+0), zítra/tomorrow (+1 day), příští týden/next week
  (+7 days). A date with no time defaults to 09:00 Europe/Prague.
- **Money** (auto-vaulted, never sent to the LLM in the clear) — only amounts
  with an explicit currency marker: `150 Kč`, `150 CZK`, `150,-`, `150k`,
  `€150`, `$150`, `150 EUR`. A bare number with no currency marker is left as
  plain text, not vaulted. Dates, times, years, and plate-style IDs (`1AB
  2345`) are protected so they're never mistaken for an amount.

See [CLAUDE.md](CLAUDE.md#triage-the-heart-appllmpy) for the full
classification rules and the LLM system prompt.
