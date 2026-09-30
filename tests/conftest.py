import os
from email.message import EmailMessage as StdMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone

import pytest

os.environ.setdefault("FRAUDALERT_DATABASE_URL", "sqlite://")
os.environ["FRAUDALERT_HOME_CURRENCY"] = "USD"
os.environ["FRAUDALERT_TIMEZONE"] = "America/New_York"
os.environ["FRAUDALERT_NOTIFY_WEBHOOK_URL"] = ""
os.environ["FRAUDALERT_WEB_USERNAME"] = ""


@pytest.fixture
def db(tmp_path):
    """Fresh database per test. Uses FRAUDALERT_TEST_DATABASE_URL (e.g. Postgres) if set."""
    from fraudalert import db as dbmod
    from fraudalert.config import get_settings

    get_settings.cache_clear()
    url = os.environ.get("FRAUDALERT_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    dbmod.configure(url)
    dbmod.Base.metadata.drop_all(dbmod.get_engine())
    dbmod.init_db()
    yield dbmod
    dbmod.Base.metadata.drop_all(dbmod.get_engine())


def make_eml(subject: str, body: str, when: datetime, sender="Bank Alerts <alerts@bank.example>", html=False) -> bytes:
    msg = StdMessage()
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when if when.tzinfo else when.replace(tzinfo=timezone.utc))
    msg["Message-ID"] = make_msgid(domain="bank.example")
    if html:
        msg.set_content("View this email in your browser.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return bytes(msg)
