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

    r = client.get("/")
    assert r.status_code == 200 and "HOTEL NOVA" in r.text and "Large or foreign purchase" in r.text
    assert client.get("/?flagged=true").text.count("HOTEL NOVA") == 1
    assert client.get("/rules").status_code == 200
    assert client.get("/emails").status_code == 200

    r = client.post("/rules", data={"name": "Hotels", "match": "all", "severity": "low",
                                    "field": ["merchant"], "op": ["contains"], "value": ["hotel"]})
    assert r.status_code == 200 and "Hotels" in r.text
    r = client.post("/rules", data={"name": "Bad", "match": "all", "field": ["amount"], "op": ["gt"], "value": ["lots"]})
    assert "expected a number" in r.text

    api = client.get("/api/rules").json()
    assert [x["name"] for x in api] == ["Large or foreign purchase", "Hotels"]
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
