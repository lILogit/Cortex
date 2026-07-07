"""Deterministic pre-LLM anonymization (Golden Rule #4).

Pure functions — no LLM, no I/O. Dates/times/years/plate-IDs are *protected*
from matching; only amounts with an explicit currency marker
(Kč/CZK/EUR/€/$/,-/k) are vaulted. Tokens are TKN- + 8 hex chars, derived from
a hash of the value so the same amount always maps to the same token
(deterministic ⇒ dedup-friendly). Generation and matching live ONLY here.
"""
import hashlib
import re

TOKEN_RE = re.compile(r"TKN-[0-9A-F]{8}")

# ---------- protected spans: numeric-looking things that are NOT money ----------

_DATE_RE = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{2,4})?(?:\s+\d{1,2}:\d{2})?")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# Czech-style plates (1AB 2345) and generic letter-digit IDs (ABC-1234).
_PLATE_RE = re.compile(r"\b\d[A-Z]{1,2}[ -]?\d{4}\b|\b[A-Z]{2,3}[ -]?\d{3,5}\b")

_PROTECT_RES = (
    ("date", _DATE_RE),
    ("time", _TIME_RE),
    ("year", _YEAR_RE),
    ("plate", _PLATE_RE),
)


def protect_patterns(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, label) spans that amount-matching must not touch."""
    spans = []
    for label, rx in _PROTECT_RES:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end(), label))
    return spans


# ---------- amount vaulting ----------

_NUM = r"\d[\d  .,]*\d|\d"
# Only amounts with an explicit currency marker are vaulted.
_AMOUNT_RE = re.compile(
    rf"(?P<cur_pre>€|\$)\s?(?P<val_pre>{_NUM})"
    rf"|(?P<val_suf>{_NUM})\s?(?P<cur_suf>Kč|Kc|CZK|EUR|€|\$|,-)"
    rf"|\b(?P<val_k>\d+(?:[.,]\d+)?)(?P<cur_k>k)\b",
    re.IGNORECASE,
)

_CURRENCY_NORM = {
    "kč": "CZK", "kc": "CZK", "czk": "CZK", ",-": "CZK", "k": "CZK",
    "eur": "EUR", "€": "EUR", "$": "USD",
}


def _token(value: str, currency: str) -> str:
    digest = hashlib.sha256(f"{value}|{currency}".encode()).hexdigest()
    return "TKN-" + digest[:8].upper()


def _overlaps(a: tuple[int, int], spans) -> bool:
    return any(a[0] < e and s < a[1] for s, e, _ in spans)


def vault_amounts(text: str, protected=None) -> tuple[str, list[dict]]:
    """Replace currency-marked amounts with TKN- tokens.

    Returns (anonymized_text, entries) where each entry is
    {token, real_value, currency, kind, record_hint}. The caller writes entries
    to the vault table — this module never touches the DB.
    """
    if protected is None:
        protected = protect_patterns(text)
    entries: list[dict] = []
    matches = []
    for m in _AMOUNT_RE.finditer(text):
        if _overlaps(m.span(), protected):
            continue
        matches.append(m)

    out = text
    for m in reversed(matches):  # right-to-left keeps earlier indices valid
        raw = m.group()
        if m.group("val_pre") is not None:
            value, cur = m.group("val_pre"), m.group("cur_pre")
        elif m.group("val_suf") is not None:
            value, cur = m.group("val_suf"), m.group("cur_suf")
        else:
            value, cur = m.group("val_k") + "k", m.group("cur_k")
        currency = _CURRENCY_NORM.get(cur.lower(), cur.upper())
        token = _token(raw.strip(), currency)
        hint = text[max(0, m.start() - 40):m.start()].strip()[-40:]
        entries.append({
            "token": token,
            "real_value": raw.strip(),
            "currency": currency,
            "kind": "amount",
            "record_hint": hint or None,
        })
        out = out[:m.start()] + token + out[m.end():]
    entries.reverse()  # restore left-to-right order
    return out, entries


def anonymize(text: str) -> tuple[str, list[dict]]:
    """protect_patterns + vault_amounts in one call."""
    return vault_amounts(text, protect_patterns(text))
