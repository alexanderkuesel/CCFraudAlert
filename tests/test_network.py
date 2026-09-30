from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fraudalert import pipeline, prefs
from fraudalert.config import get_settings
from fraudalert.models import Transaction
from fraudalert.network import build_network
from fraudalert.web.app import create_app

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def add(s, merchant, card, amount, currency="USD", days_ago=1, priority=None, **kw):
    """priority='high'|'medium'|'low' attaches an alarm of that priority (and flags the transaction)."""
    from fraudalert.models import Alert

    t = Transaction(merchant=merchant, card_last4=card, amount=amount, currency=currency,
                    occurred_at=NOW - timedelta(days=days_ago), is_foreign=kw.pop("is_foreign", False), **kw)
    if priority:
        t.flagged = True
        t.alerts.append(Alert(reason="test alarm", severity=priority))
    s.add(t)
    return t


def graph(db, days=90):
    with db.session_scope() as s:
        return build_network(s, pipeline.Env.load(s, get_settings()), days, now=NOW)


def test_nodes_edges_and_signals(db):
    with db.session_scope() as s:
        prefs.set_normal_currencies(s, ["CRC", "USD"])
        for d in (60, 40, 20):
            add(s, "AUTOMERCADO", "1111", 30, days_ago=d)
        add(s, "automercado ", "2222", 50, days_ago=5)  # same merchant (normalised), second card
        add(s, "SHOPXYZ", "1111", 2, days_ago=2, priority="high")  # new + unacknowledged High alarm
        add(s, "HOTEL LTD", "2222", 640, currency="EUR", days_ago=9, priority="medium", label_fraud=True)
        add(s, "GLOBAL-E", "1111", 154, days_ago=30, priority="medium", label_fraud=False, is_foreign=True)
        add(s, "SODA", None, 10_000, currency="CRC", days_ago=3, anomaly_score=0.9)  # high score but no alarm
        add(s, "OLD SHOP", "1111", 5, days_ago=200)  # outside the 90-day window
    g = graph(db)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert {n["label"] for n in g["nodes"] if n["kind"] == "card"} == {"Card …1111", "Card …2222", "Unknown card"}
    am = nodes["m:automercado"]
    assert (am["count"], am["cards"], am["state"], am["new"], am["foreign"]) == (4, 2, "normal", False, False)
    assert am["total"] == 140.0
    assert nodes["m:shopxyz"]["state"] == "p1" and nodes["m:shopxyz"]["new"]
    hotel = nodes["m:hotel ltd"]
    assert hotel["state"] == "fraud" and hotel["foreign"]  # EUR isn't a normal currency
    assert hotel["total"] > 640  # converted to USD
    assert nodes["m:global-e"]["state"] == "legit" and nodes["m:global-e"]["foreign"]
    # ISA-18.2: only alarm rules raise alarms; a high anomaly score alone doesn't colour the node
    assert nodes["m:soda"]["state"] == "normal" and nodes["m:soda"]["max_score"] == 0.9
    assert nodes["m:soda"]["total"] < 100  # 10,000 CRC in USD
    assert "m:old shop" not in nodes
    edges = {(e["source"], e["target"]): e for e in g["edges"]}
    assert edges[("card:1111", "m:automercado")]["count"] == 3
    assert edges[("card:1111", "m:shopxyz")]["flagged"] == 1
    assert ("card:????", "m:soda") in edges


def test_new_merchant_uses_all_history_and_worst_state_wins(db):
    with db.session_scope() as s:
        add(s, "NETFLIX", "1111", 15, days_ago=400)  # seen long ago -> not new, even though the window is 30 days
        add(s, "NETFLIX", "1111", 15, days_ago=3)
        add(s, "MIXED", "1111", 20, days_ago=4, priority="high", label_fraud=False)
        add(s, "MIXED", "1111", 20, days_ago=3, priority="low")  # unacknowledged beats acknowledged-legit
    nodes = {n["id"]: n for n in graph(db, days=30)["nodes"]}
    assert not nodes["m:netflix"]["new"]
    assert nodes["m:mixed"]["state"] == "p3"
    assert {n["id"] for n in graph(db, days=None)["nodes"]} >= {"m:netflix", "m:mixed"}


def test_network_page_and_api(db):
    with db.session_scope() as s:
        add(s, "<script>alert(1)</script>", "1111", 5, days_ago=1)
    client = TestClient(create_app(init=False))
    page = client.get("/network")
    assert page.status_code == 200 and "network.js" in page.text and "Network" in page.text
    data = client.get("/api/network?days=all").json()
    assert data["home_currency"] == "USD" and len(data["nodes"]) == 2
    assert client.get("/api/network?days=7").status_code == 422
    assert client.get("/static/network.js").status_code == 200
