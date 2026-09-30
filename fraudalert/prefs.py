"""User preferences editable from the web UI. Stored in the database (sync_state table); the
matching FRAUDALERT_* environment variable is only the default until you save one in the UI."""

import re

from sqlalchemy.orm import Session

from fraudalert.config import Settings
from fraudalert.models import SyncState

NORMAL_CURRENCIES = "pref:normal_currencies"


def parse_currency_list(text: str) -> list[str]:
    """'crc, usd' -> ['CRC', 'USD']. Raises ValueError on anything that isn't a 3-letter code."""
    codes = [c.strip().upper() for c in re.split(r"[,\s]+", text) if c.strip()]
    bad = [c for c in codes if not re.fullmatch(r"[A-Z]{3}", c)]
    if bad:
        raise ValueError(f"not a 3-letter currency code: {', '.join(bad)}")
    if not codes:
        raise ValueError("list at least one currency")
    return list(dict.fromkeys(codes))  # dedupe, keep order


def default_normal_currencies(settings: Settings) -> list[str]:
    return parse_currency_list(settings.normal_currencies) if settings.normal_currencies.strip() else [
        settings.home_currency.upper()
    ]


def normal_currencies(session: Session, settings: Settings) -> list[str]:
    row = session.get(SyncState, NORMAL_CURRENCIES)
    return parse_currency_list(row.value) if row else default_normal_currencies(settings)


def set_normal_currencies(session: Session, codes: list[str]) -> None:
    value = ",".join(codes)
    row = session.get(SyncState, NORMAL_CURRENCIES)
    if row:
        row.value = value
    else:
        session.add(SyncState(key=NORMAL_CURRENCIES, value=value))
