from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fraudalert import pipeline
from fraudalert.web.app import create_app

from .conftest import make_eml


def test_pages_and_rule_crud(db, tmp_path):
    p = tmp_path / "a.eml"
    p.write_bytes(make_eml("Alert", "You spent $250.00 at HOTEL NOVA.", datetime.now(timezone.utc) - timedelta(days=1)))
    pipeline.import_eml_files([p])
    client = TestClient(create_app(init=False))

    r = client.get("/alarms")
    assert r.status_code == 200 and "HOTEL NOVA" in r.text and "Large or foreign purchase" in r.text
    assert client.get("/alarms?flagged=true").text.count('aria-label="Select HOTEL NOVA"') == 1  # one row
    assert client.get("/rules").status_code == 200
    assert client.get("/emails").status_code == 200

    r = client.post("/rules", data={"name": "Hotels", "match": "all", "severity": "low",
                                    "field": ["merchant"], "op": ["contains"], "value": ["hotel"]})
    assert r.status_code == 200 and "Hotels" in r.text
    r = client.post("/rules", data={"name": "Bad", "match": "all", "field": ["amount"], "op": ["gt"], "value": ["lots"]})
    assert "expected a number" in r.text

    api = client.get("/api/rules").json()
    assert [x["name"] for x in api] == [
        "Large or foreign purchase", "Card test (zero/near-zero amount)", "Charge after a card test",
        "Unusual pattern (anomaly model)", "Hotels"]
    r = client.post("/api/rules", json={"name": "Night", "conditions": [{"field": "hour", "op": "lt", "value": 5}]})
    assert r.status_code == 201
    assert client.post("/api/rules", json={"name": "x", "conditions": []}).status_code == 422

    txn = client.get("/api/transactions").json()[0]
    assert set(txn["alerts"]) == {"Large or foreign purchase: amount > 100 OR is_foreign = true",
                                  "Hotels: merchant contains hotel"}
    client.post(f"/transactions/{txn['id']}/label", data={"label": "legit"})
    assert client.get("/api/transactions").json()[0]["label_fraud"] is False

    for rule in client.get("/api/rules").json():
        assert client.delete(f"/api/rules/{rule['id']}").status_code == 204
    assert client.get("/api/transactions?flagged=true").json() == []


def test_basic_auth(db, monkeypatch):
    from fraudalert.config import get_settings

    monkeypatch.setenv("FRAUDALERT_WEB_USERNAME", "me")
    monkeypatch.setenv("FRAUDALERT_WEB_PASSWORD", "s3cret")
    get_settings.cache_clear()
    client = TestClient(create_app(init=False))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("me", "s3cret")).status_code == 200
    get_settings.cache_clear()


def test_cross_site_posts_blocked(db):
    client = TestClient(create_app(init=False))
    form = {"name": "x", "match": "all", "field": ["amount"], "op": ["gt"], "value": ["1"]}
    r = client.post("/rules", data=form, headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/rules/1/delete", headers={"referer": "http://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.post("/rules", data=form, headers={"origin": "http://testserver"}, follow_redirects=False).status_code == 303
    assert client.get("/", headers={"origin": "https://evil.example"}).status_code == 200  # reads unaffected


def test_refuses_network_exposure_without_password():
    from fraudalert.cli import exposure_problem
    from fraudalert.config import Settings

    open_ = Settings(web_bind="0.0.0.0", web_username="", web_password="")
    locked = Settings(web_bind="0.0.0.0", web_username="me", web_password="pw")
    local = Settings(web_bind="127.0.0.1", web_username="", web_password="")
    assert exposure_problem(open_, "0.0.0.0", in_container=True)
    assert exposure_problem(locked, "0.0.0.0", in_container=True) is None
    assert exposure_problem(local, "0.0.0.0", in_container=True) is None  # docker publishes on loopback only
    assert exposure_problem(local, "0.0.0.0", in_container=False)  # bare `serve --host 0.0.0.0`
    assert exposure_problem(local, "127.0.0.1", in_container=False) is None


def test_reparse_all_button(db, tmp_path):
    from .bac import bac_eml

    p = tmp_path / "bac.eml"
    p.write_bytes(bac_eml(datetime.now(timezone.utc) - timedelta(days=1)))
    pipeline.import_eml_files([p])
    client = TestClient(create_app(init=False))
    assert "Re-parse all emails" in client.get("/emails").text
    r = client.post("/emails/reparse-all")
    assert r.status_code == 200 and "1 transactions, 0 unparsed" in r.text


def test_settings_page_normal_currencies(db):
    from fraudalert import prefs
    from fraudalert.config import get_settings

    client = TestClient(create_app(init=False))
    assert "Normal currencies" in client.get("/settings").text
    r = client.post("/settings", data={"normal_currencies": "crc, usd"})
    assert r.status_code == 200 and "Normal currencies: CRC, USD" in r.text
    with db.session_scope() as s:
        assert prefs.normal_currencies(s, get_settings()) == ["CRC", "USD"]
    r = client.post("/settings", data={"normal_currencies": "colones"})
    assert "not a 3-letter currency code: COLONES" in r.text
    with db.session_scope() as s:
        assert prefs.normal_currencies(s, get_settings()) == ["CRC", "USD"]  # unchanged


def test_review_styling_and_comments(db, tmp_path):
    now = datetime.now(timezone.utc)
    files = []
    for i, merchant in enumerate(["FRAUDY", "LEGITCO", "PENDING"]):
        p = tmp_path / f"{i}.eml"
        p.write_bytes(make_eml("Alert", f"You spent $500.00 at {merchant}.", now - timedelta(days=3 - i)))
        files.append(p)
    pipeline.import_eml_files(files)
    client = TestClient(create_app(init=False))
    ids = {t["merchant"]: t["id"] for t in client.get("/api/transactions").json()}
    client.post(f"/transactions/{ids['FRAUDY']}/label", data={"label": "fraud"})
    client.post(f"/transactions/{ids['LEGITCO']}/label", data={"label": "legit"})

    # Inline save (fetch) returns a small OK; a plain form post redirects back.
    r = client.post(f"/transactions/{ids['FRAUDY']}/comment", data={"comment": "  not me — card cancelled  "},
                    headers={"X-Requested-With": "fetch"})
    assert (r.status_code, r.text) == (200, "saved")
    assert client.post(f"/transactions/{ids['LEGITCO']}/comment", data={"comment": "x" * 5000},
                       follow_redirects=False).status_code == 303
    assert client.post("/transactions/999999/comment", data={"comment": "x"}).status_code == 404

    by = {t["merchant"]: t for t in client.get("/api/transactions").json()}
    assert by["FRAUDY"]["comment"] == "not me — card cancelled"
    assert len(by["LEGITCO"]["comment"]) == 1000
    assert by["PENDING"]["comment"] is None

    html = client.get("/alarms?view=journal").text
    rows = {m: html.split(m)[0].rsplit("<tr", 1)[1] for m in ("FRAUDY", "LEGITCO", "PENDING")}
    assert 'class="row-fraud' in rows["FRAUDY"]
    assert 'class="row-legit' in rows["LEGITCO"]
    assert 'class="row-unack' in rows["PENDING"]  # $500 matches the default rule, not acknowledged yet
    assert "✗ FRAUD" in html and "✓ LEGIT" in html
    assert 'value="not me — card cancelled"' in html

    client.post(f"/transactions/{ids['FRAUDY']}/comment", data={"comment": ""})  # clearing
    assert {t["merchant"]: t["comment"] for t in client.get("/api/transactions").json()}["FRAUDY"] is None


def test_alarm_summary_views_banner_and_priorities(db, tmp_path):
    """ISA-18.2 alarm summary: unacknowledged first by priority, banner counts, acknowledge, priority edits."""
    from fraudalert.models import Rule
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    files = []
    for i, (merchant, amount) in enumerate([("CARD TESTER", "0.00"), ("BIG TV", "900.00"), ("COFFEE", "4.50")]):
        p = tmp_path / f"{i}.eml"
        p.write_bytes(make_eml("Alert", f"You spent ${amount} at {merchant}.", now - timedelta(hours=10 - i)))
        files.append(p)
    pipeline.import_eml_files(files)
    client = TestClient(create_app(init=False))

    html = client.get("/alarms").text  # default view = unacknowledged
    assert "Finance Trends &amp; Alarms" in html and "Passive" in html
    assert "COFFEE" not in html  # no alarm -> not in the alarm summary
    assert html.index("CARD TESTER") < html.index("BIG TV")  # High (card test) sorts before Medium
    assert 'title="Priority 1 · High"' in html and 'title="Priority 2 · Medium"' in html
    assert "Unacknowledged alarms" in html  # banner
    assert "COFFEE" in client.get("/alarms?view=journal").text

    ids = {t["merchant"]: t["id"] for t in client.get("/api/transactions").json()}
    client.post(f"/transactions/{ids['CARD TESTER']}/label", data={"label": "fraud"})
    client.post(f"/transactions/{ids['BIG TV']}/label", data={"label": "legit"})
    html = client.get("/alarms").text
    assert "All clear." in html and "No unacknowledged alarms" in html
    alarms = client.get("/alarms?view=alarms").text
    assert "CARD TESTER" in alarms and "BIG TV" in alarms and "✗ FRAUD" in alarms and "✓ LEGIT" in alarms
    assert client.get("/alarms?flagged=true").text.count("BIG TV") >= 1  # old links still work

    with db.session_scope() as s:
        rule_id = s.scalar(select(Rule.id).where(Rule.name == "Large or foreign purchase"))
    r = client.post(f"/rules/{rule_id}/priority", data={"severity": "low"})
    assert "is now Low priority" in r.text
    assert "priority must be one of" in client.post(f"/rules/{rule_id}/priority", data={"severity": "urgent"}).text
    client.post(f"/transactions/{ids['BIG TV']}/label", data={"label": ""})  # un-acknowledge
    assert 'title="Priority 3 · Low"' in client.get("/alarms").text


def test_notification_leads_with_priority(db, tmp_path, monkeypatch):
    import httpx

    from fraudalert.config import get_settings

    sent = []
    monkeypatch.setenv("FRAUDALERT_NOTIFY_WEBHOOK_URL", "https://hooks.example/x")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: sent.append(json) or httpx.Response(200, request=httpx.Request("POST", url)))
    p = tmp_path / "t.eml"
    p.write_bytes(make_eml("Alert", "You spent $0.00 at CARD TESTER.", datetime.now(timezone.utc) - timedelta(hours=1)))
    pipeline.import_eml_files([p])
    assert sent and sent[0]["priority"] == "HIGH" and sent[0]["text"].startswith("[HIGH] Transaction alarm")
    get_settings.cache_clear()


def test_static_assets_are_versioned_by_content(db):
    import hashlib
    import re
    from pathlib import Path

    client = TestClient(create_app(init=False))
    static = Path(__file__).parents[1] / "fraudalert" / "web" / "static"
    for page, asset in [("/", "style.css"), ("/spending", "spending.js"), ("/network", "network.js")]:
        html = client.get(page).text
        m = re.search(rf'/static/{re.escape(asset)}\?v=([0-9a-f]+)"', html)
        assert m, f"{page} doesn't link a versioned {asset}"
        assert m.group(1) == hashlib.sha256((static / asset).read_bytes()).hexdigest()[:12]
        r = client.get(f"/static/{asset}?v={m.group(1)}")
        assert r.status_code == 200 and r.content == (static / asset).read_bytes()


def test_overview_is_the_home_page_and_old_alarm_links_redirect(db, tmp_path):
    now = datetime.now(timezone.utc)
    files = []
    for i, (merchant, amount) in enumerate([("AUTOMERCADO", "42.10"), ("BIG TV", "900.00")]):
        p = tmp_path / f"{i}.eml"
        p.write_bytes(make_eml("Alert", f"You spent ${amount} at {merchant}.", now - timedelta(hours=5 - i)))
        files.append(p)
    pipeline.import_eml_files(files)
    client = TestClient(create_app(init=False))

    html = client.get("/").text
    assert "<h1>Overview</h1>" in html and "overview.js" in html and "charts.js" in html
    assert "Recent transactions" in html and "AUTOMERCADO" in html and "Groceries" in html  # labelled with its category
    assert 'class="nav-count p2"' in html  # BIG TV raised a medium alarm: counted on the Alarms nav item
    assert "BIG TV" in html.split('id="ov-alarms-h"')[1]  # and listed in the alarms card
    assert client.get("/?msg=Synced").status_code == 200  # flash messages stay on the overview

    r = client.get("/?view=unack&q=tv", follow_redirects=False)  # old bookmarks and report links
    assert r.status_code == 307 and r.headers["location"] == "/alarms?view=unack&q=tv"
    assert "BIG TV" in client.get("/alarms?view=unack").text


def test_overview_without_data(db):
    html = TestClient(create_app(init=False)).get("/").text
    assert "No transactions yet" in html and "All clear." in html
