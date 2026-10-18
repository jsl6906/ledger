import hashlib
import re
from datetime import date
from decimal import Decimal

_NOISE = re.compile(
    r"""
    \b(pos|debit|credit|purchase|recurring|payment|card|checkcard|visa|mastercard|ach|web|pmt|ppd|id|ref|des|indn|co)\b
    | \$?\b\d[\d,]*\.\d{2}\b   # embedded amounts like 'Future Amount: 212.73'
    | \b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(/\d{2,4})?\b   # posting dates like 04/24
    | \#\s*\d+             # store numbers like #412
    | \b\d{3,}\b           # long digit runs (refs, store ids, phone)
    | \*[a-z0-9]+          # AMAZON.COM*2K4 style suffixes
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NON_WORD = re.compile(r"[^a-z0-9&' ]+")
_SPACES = re.compile(r"\s+")
# Checked after the noise is gone so a code followed by a reference number ('... IL 2443106') still goes.
_TRAILING_STATE = re.compile(r" [a-z]{2}$")
# Bank shorthand spelled out so it matches the long form ('BANK OF AMERICA ...').
_EXPANSIONS = {"bkofamerica": "bank of america", "withdrwl": "withdrawal"}


def normalize_merchant(description: str | None) -> str | None:
    """Collapse a raw bank description to a stable merchant key ('KROGER #412 SPRINGFIELD IL' -> 'kroger springfield')."""
    if not description:
        return None
    s = description.lower()
    s = _NOISE.sub(" ", s)
    s = _NON_WORD.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    s = _TRAILING_STATE.sub("", s)
    s = " ".join(_EXPANSIONS.get(w, w) for w in s.split())
    return s[:200] or None


def fingerprint(account_id: int | None, txn_date: date, amount: Decimal, description: str | None) -> str:
    key = f"{account_id or 0}|{txn_date.isoformat()}|{Decimal(amount):.2f}|{normalize_merchant(description) or ''}"
    return hashlib.sha256(key.encode()).hexdigest()
