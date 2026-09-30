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
