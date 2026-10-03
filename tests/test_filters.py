from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fraudalert import pipeline
from fraudalert.config import get_settings
from fraudalert.models import Alert, Rule, Transaction
from fraudalert.web.app import create_app
from fraudalert.web.filters import Filters

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def data(db):
    """A small ledger: two cards, alarms of each priority, acknowledged and not, CRC and USD."""
    from fraudalert import prefs

    with db.session_scope() as s:
        prefs.set_normal_currencies(s, ["CRC", "USD"])  # colones are normal; EUR is foreign
        rules = {r.severity: r for r in s.scalars(select(Rule))}  # built-ins: medium + two high
        low = Rule(name="Night owl", match="all", conditions=[{"field": "hour", "op": "lt", "value": 5}], severity="low")
        s.add(low)
        s.flush()

        def add(merchant, amount, currency="USD", days=1, card="1111", sev=None, label=None, score=None):
            t = Transaction(merchant=merchant, amount=amount, currency=currency, card_last4=card,
                            occurred_at=NOW - timedelta(days=days), label_fraud=label, anomaly_score=score,
                            is_foreign=currency == "EUR")
            if sev:
                rule = low if sev == "low" else rules[sev]
                t.flagged = True
                t.alerts.append(Alert(rule_id=rule.id, reason=f"{rule.name}: x", severity=sev))
            s.add(t)

        add("CARD TEST", 0, sev="high", score=0.9)
        add("NETFLIX", 15.49, days=2, sev="medium")
        add("NETFLIX", 15.49, days=32, sev="medium", label=False)
        add("HOTEL", 640, "EUR", days=5, card="2222", sev="medium", label=True)
        add("SODA", 5000, "CRC", days=3, card="2222", score=0.1)
        add("LATE SNACK", 3, days=4, sev="low")
    return db


def ids(db, qs):
    with db.session_scope() as s:
        f = Filters.from_query_string(qs)
        return [t.merchant for t in f.apply(s, pipeline.Env.load(s, get_settings()))]


def test_view_priority_state_and_sort(data):
    assert ids(data, "view=unack") == ["CARD TEST", "NETFLIX", "LATE SNACK"]  # priority, then newest
    assert ids(data, "view=alarms&pri=2") == ["NETFLIX", "HOTEL", "NETFLIX"]  # unack before acknowledged
    assert ids(data, "view=journal&pri=none") == ["SODA"]
    assert ids(data, "view=journal&state=fraud&state=legit") == ["HOTEL", "NETFLIX"]  # journal: newest first
    assert ids(data, "view=journal&state=none") == ["SODA"]


def test_date_amount_anomaly_card_foreign_rule_and_text(data):
    start = (NOW - timedelta(days=6)).date().isoformat()
    assert "NETFLIX" in ids(data, f"view=journal&from={start}")
    assert len([m for m in ids(data, f"view=journal&from={start}") if m == "NETFLIX"]) == 1  # 32-day one excluded
    # amounts compare in the home currency (USD): 5,000 CRC ~ 10 USD, 640 EUR ~ 700 USD
    assert sorted(ids(data, "view=journal&amin=5&amax=20")) == ["NETFLIX", "NETFLIX", "SODA"]
    assert ids(data, "view=journal&amin=500") == ["HOTEL"]
    assert ids(data, "view=journal&anom=0.5") == ["CARD TEST"]
    assert sorted(ids(data, "view=journal&card=2222")) == ["HOTEL", "SODA"]
    assert ids(data, "view=journal&foreign=yes") == ["HOTEL"]
    with data.session_scope() as s:
        night = s.scalar(select(Rule.id).where(Rule.name == "Night owl"))
    assert ids(data, f"view=journal&rule={night}") == ["LATE SNACK"]
    assert ids(data, "view=journal&q=netf") == ["NETFLIX", "NETFLIX"]


def test_bad_input_is_reported_not_crashing(data):
    f = Filters.from_query_string("view=bogus&amin=lots&from=yesterday&pri=9&state=maybe&rule=x")
    assert f.view == "unack" and f.amount_min is None and f.date_from is None and not f.pri and not f.state
    assert f.errors == ["From must be a date (YYYY-MM-DD)", "Min amount must be a number"]
    client = TestClient(create_app(init=False))
    r = client.get("/alarms?view=journal&amin=lots")
    assert r.status_code == 200 and "Min amount must be a number" in r.text


def test_query_string_round_trip_and_chips(data):
    qs = "view=alarms&q=net&pri=1&pri=2&state=unack&from=2026-09-01&to=2026-09-30&amin=1&amax=99.5&anom=0.2&card=1111&rule=3&foreign=no"
    f = Filters.from_query_string(qs)
    assert Filters.from_query_string(f.query_string()) == f
    chips = dict(f.chips({3: "Charge after a card test"}, "USD"))
    assert "Priority: High, Medium" in chips and "Alarm: Charge after a card test" in chips
    assert "pri=" not in chips["Priority: High, Medium"]  # the chip's link removes that filter only
    assert "card=1111" in chips["Priority: High, Medium"]


def test_page_filters_and_bulk_edit(data):
    client = TestClient(create_app(init=False))
    html = client.get("/alarms?view=journal&card=2222").text
    assert 'aria-label="Select HOTEL"' in html and 'aria-label="Select SODA"' in html
    assert 'aria-label="Select NETFLIX"' not in html
    assert "Card …2222" in html and "2 matches" in html

    netflix = [t["id"] for t in client.get("/api/transactions").json() if t["merchant"] == "NETFLIX"]
    # selected rows
    r = client.post("/transactions/bulk", data={"action": "legit", "ids": netflix, "filters": "view=unack"})
    assert "Acknowledged 2 transactions as legit." in r.text
    # "select all N matching": everything unacknowledged with a Low or High alarm -> fraud
    r = client.post("/transactions/bulk", data={"action": "fraud", "all_matching": "1", "filters": "view=unack&pri=1&pri=3"})
    assert "Acknowledged 2 transactions as fraud." in r.text
    by = {}
    for t in client.get("/api/transactions").json():
        by.setdefault(t["merchant"], set()).add(t["label_fraud"])
    assert by["NETFLIX"] == {False} and by["CARD TEST"] == {True} and by["LATE SNACK"] == {True}
    assert by["SODA"] == {None}  # untouched
    # comment + clear, and the redirect keeps the filters
    r = client.post("/transactions/bulk", data={"action": "comment", "comment": " reviewed Oct ", "ids": netflix,
                                                "filters": "view=journal&q=netflix"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/alarms?view=journal&q=netflix")
    r = client.post("/transactions/bulk", data={"action": "clear", "ids": netflix, "filters": ""})
    assert "Cleared the acknowledgement on 2 transactions." in r.text
    rows = [t for t in client.get("/api/transactions").json() if t["merchant"] == "NETFLIX"]
    assert {t["comment"] for t in rows} == {"reviewed Oct"} and {t["label_fraud"] for t in rows} == {None}
    # nothing selected / bad action
    assert "nothing selected" in client.post("/transactions/bulk", data={"action": "legit"}).text
    assert "choose a bulk action" in client.post("/transactions/bulk", data={"action": "delete", "ids": netflix}).text


def test_bulk_api(data):
    client = TestClient(create_app(init=False))
    all_ids = [t["id"] for t in client.get("/api/transactions").json()]
    assert client.post("/api/transactions/bulk", json={"ids": all_ids[:2], "action": "fraud"}).json() == {"updated": 2}
    assert client.post("/api/transactions/bulk", json={"ids": [999999], "action": "legit"}).json() == {"updated": 0}
    assert client.post("/api/transactions/bulk", json={"ids": all_ids, "action": "nuke"}).status_code == 422
    # cross-site form posts are still blocked for the bulk endpoint
    r = client.post("/transactions/bulk", data={"action": "fraud", "ids": all_ids}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
