from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fraudalert import pipeline, spending
from fraudalert.anomaly.features import merchant_key
from fraudalert.config import get_settings
from fraudalert.models import Category, MerchantTag, Transaction
from fraudalert.web.app import create_app

# Mid-month, mid-day in New York (the test timezone): day 15 of a 30-day month.
NOW = datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)


def add(s, merchant, amount, when, currency="USD", fraud=None):
    s.add(Transaction(merchant=merchant, amount=amount, currency=currency, card_last4="4321",
                      occurred_at=when, label_fraud=fraud))


def env(s):
    return pipeline.Env.load(s, get_settings())


def device(ov, name):
    return next(d for d in ov["devices"] if d["name"] == name)


@pytest.mark.parametrize("merchant,expected", [
    ("UBER EATS SAN JOSE", "Dining"), ("UBER *TRIP", "Transport"), ("AUTOMERCADO ESCAZU", "Groceries"),
    ("AMAZON.COM LLC", "Shopping"), ("NETFLIX.COM", "Subscriptions"), ("FARMACIA FISCHEL", "Health"),
    ("Café Britt", "Dining"), ("BARBERIA EL CORTE", None), ("SERVICE CENTER", None), ("BAR LA CALI", "Dining"),
])
def test_guess_category(merchant, expected):
    assert spending.guess_category(merchant) == expected


def test_seed_once_and_sync_tags(db):
    with db.session_scope() as s:
        add(s, "UBER EATS", 12, NOW)
        add(s, "MYSTERY SHOP", 5, NOW)
        s.flush()
        assert spending.sync_tags(s) == 2
        assert spending.sync_tags(s) == 0
        names = list(s.scalars(select(Category.name).order_by(Category.sort)))
        assert names[:2] == ["Dining", "Groceries"] and len(names) == len(spending.DEFAULT_CATEGORIES)
        tags = {t.merchant_key: t for t in s.scalars(select(MerchantTag))}
        assert tags[merchant_key("UBER EATS")].category.name == "Dining"
        assert tags[merchant_key("MYSTERY SHOP")].category_id is None
        # a deleted default category is not re-created
        spending.delete_category(s, s.scalar(select(Category.id).where(Category.name == "Travel")))
        spending.seed_categories(s)
        assert s.scalar(select(Category.id).where(Category.name == "Travel")) is None


def test_category_crud_and_validation(db):
    with db.session_scope() as s:
        spending.seed_categories(s)
        kids = spending.create_category(s, "  Kids  ", "1,200.5")
        assert kids.name == "Kids" and float(kids.budget_monthly) == 1200.5
        for bad in ("", "uncategorized", "kids"):
            with pytest.raises(ValueError):
                spending.create_category(s, bad)
        with pytest.raises(ValueError, match="number"):
            spending.create_category(s, "Pets", "lots")
        with pytest.raises(ValueError, match="negative"):
            spending.update_category(s, kids.id, budget="-1")
        with pytest.raises(ValueError, match="already exists"):
            spending.update_category(s, kids.id, name="DINING")
        spending.update_category(s, kids.id, name="Children")
        assert kids.name == "Children" and float(kids.budget_monthly) == 1200.5  # budget kept
        spending.update_category(s, kids.id, budget="")
        assert kids.budget_monthly is None
        with pytest.raises(LookupError):
            spending.update_category(s, 9999, name="x")


def test_user_assignment_wins_and_delete_moves_to_uncategorized(db):
    with db.session_scope() as s:
        add(s, "UBER EATS", 12, NOW)
        s.flush()
        spending.sync_tags(s)
        key = merchant_key("UBER EATS")
        groceries = s.scalar(select(Category.id).where(Category.name == "Groceries"))
        spending.assign(s, [key], groceries)
        spending.sync_tags(s)  # auto-tagging never overwrites
        tag = s.scalar(select(MerchantTag).where(MerchantTag.merchant_key == key))
        assert tag.category_id == groceries and tag.assigned_by == "user"
        assert spending.delete_category(s, groceries) == 1
        s.flush()
        s.refresh(tag)
        assert tag.category_id is None and tag.assigned_by == "user"
        with pytest.raises(LookupError):
            spending.assign(s, [key], 9999)


def test_overview_limits_projection_and_fraud_excluded(db):
    with db.session_scope() as s:
        spending.seed_categories(s)
        dining = s.scalar(select(Category).where(Category.name == "Dining"))
        groceries = s.scalar(select(Category).where(Category.name == "Groceries"))
        dining.budget_monthly, groceries.budget_monthly = 100, 1000
        add(s, "UBER EATS", 50, NOW - timedelta(days=3))
        add(s, "PIZZA HUT", 35, NOW - timedelta(days=1))           # dining 85 -> HI (>= 80%)
        add(s, "PIZZA HUT", 500, NOW - timedelta(days=2), fraud=True)  # excluded
        add(s, "AUTOMERCADO", 300, NOW - timedelta(days=5))
        add(s, "AUTOMERCADO", 900, NOW - timedelta(days=40))       # last month
        add(s, "MYSTERY SHOP", 0, NOW)                             # zero-amount card test: not spending
        add(s, "MYSTERY SHOP", 20, NOW - timedelta(hours=1))

    with db.session_scope() as s:
        ov = spending.overview(s, env(s), now=NOW)
    assert ov["currency"] == "USD" and ov["month"] == "2026-06-01"
    assert ov["day_of_month"] == 15 and ov["days_in_month"] == 30
    d = device(ov, "Dining")
    assert d["mtd"] == 85 and d["status"] == "hi" and d["pct"] == 0.85 and d["projected"] == 170
    assert [t["name"] for t in d["tags"]] == ["UBER EATS", "PIZZA HUT"]
    assert d["tags"][1]["count"] == 1
    g = device(ov, "Groceries")
    assert g["mtd"] == 300 and g["last_month"] == 900 and g["status"] == "ok" and g["spark"][-2:] == [900, 300]
    assert device(ov, "Travel")["status"] == "none"
    u = device(ov, spending.UNCATEGORIZED)
    assert u["id"] is None and u["mtd"] == 20 and u["tags"][0]["count"] == 1
    assert ov["total"]["mtd"] == 405 and ov["total"]["budget"] == 1100

    with db.session_scope() as s:
        add(s, "PIZZA HUT", 20, NOW - timedelta(hours=2))
    with db.session_scope() as s:
        assert device(spending.overview(s, env(s), now=NOW), "Dining")["status"] == "hihi"


def test_series_buckets_and_month_to_date(db):
    with db.session_scope() as s:
        spending.seed_categories(s)
        s.scalar(select(Category).where(Category.name == "Dining")).budget_monthly = 100
        add(s, "UBER EATS", 10, datetime(2026, 6, 1, 16, tzinfo=timezone.utc))
        add(s, "UBER EATS", 15, datetime(2026, 6, 3, 16, tzinfo=timezone.utc))
        add(s, "PIZZA HUT", 70, datetime(2026, 6, 14, 16, tzinfo=timezone.utc))
        add(s, "PIZZA HUT", 40, datetime(2026, 5, 20, 16, tzinfo=timezone.utc))
        # 01:00 UTC on Jun 1 is still May 31 in New York
        add(s, "PIZZA HUT", 5, datetime(2026, 6, 1, 1, tzinfo=timezone.utc))
        add(s, "AUTOMERCADO", 999, datetime(2026, 6, 2, 16, tzinfo=timezone.utc))

    with db.session_scope() as s:
        cid = s.scalar(select(Category.id).where(Category.name == "Dining"))
        day = spending.series(s, env(s), category=cid, bucket="day", days=30, now=NOW)
        assert day["label"] == "Dining" and day["budget"] == 100 and len(day["points"]) == 30
        by = {p["start"]: p for p in day["points"]}
        assert by["2026-06-14"]["value"] == 70 and by["2026-05-31"]["value"] == 5 and by["2026-06-01"]["count"] == 1
        m = day["mtd"]
        assert m["days_in_month"] == 30 and len(m["cumulative"]) == 15
        assert m["cumulative"][0] == 10 and m["cumulative"][2] == 25 and m["cumulative"][-1] == 95
        assert m["status"] == "hi"

        month = spending.series(s, env(s), category=str(cid), bucket="month", days=90, now=NOW)
        assert [(p["start"], p["value"]) for p in month["points"]] == [
            ("2026-04-01", 0), ("2026-05-01", 45), ("2026-06-01", 95)]

        week = spending.series(s, env(s), bucket="week", days=14, now=NOW)
        assert all(datetime.fromisoformat(p["start"]).weekday() == 0 for p in week["points"])
        assert week["label"] == "All spending" and week["budget"] == 100

        tag = spending.series(s, env(s), merchant=merchant_key("PIZZA HUT"), bucket="day", days=30, now=NOW)
        assert tag["label"] == "PIZZA HUT" and tag["device"] == "Dining" and tag["budget"] is None
        assert tag["mtd"]["cumulative"][-1] == 70

        unc = spending.series(s, env(s), category="uncategorized", now=NOW)
        assert unc["label"] == spending.UNCATEGORIZED and unc["mtd"]["cumulative"][-1] == 0
        with pytest.raises(ValueError):
            spending.series(s, env(s), bucket="hour")
        with pytest.raises(LookupError):
            spending.series(s, env(s), category=9999)


def test_spending_api(db):
    with db.session_scope() as s:
        add(s, "UBER EATS", 12, datetime.now(timezone.utc) - timedelta(hours=1))
        add(s, "MYSTERY SHOP", 8, datetime.now(timezone.utc) - timedelta(hours=1))
    client = TestClient(create_app(init=False))
    page = client.get("/spending")
    assert page.status_code == 200 and "Spend historian" in page.text and "spending.js" in page.text

    ov = client.get("/api/spending/overview").json()
    assert device(ov, "Dining")["mtd"] == 12 and device(ov, spending.UNCATEGORIZED)["mtd"] == 8

    r = client.post("/api/spending/categories", json={"name": "Hobbies", "budget": "50"})
    assert r.status_code == 201
    hid = r.json()["id"]
    assert client.post("/api/spending/categories", json={"name": "hobbies"}).status_code == 422
    assert client.patch(f"/api/spending/categories/{hid}", json={"budget": "x"}).status_code == 422
    r = client.patch(f"/api/spending/categories/{hid}", json={"budget": "5"})
    assert r.json() == {"id": hid, "name": "Hobbies", "budget": 5.0}
    assert client.patch("/api/spending/categories/9999", json={"name": "x"}).status_code == 404

    key = merchant_key("MYSTERY SHOP")
    assert client.post("/api/spending/assign", json={"keys": [key], "category_id": hid}).json() == {"assigned": 1}
    assert client.post("/api/spending/assign", json={"keys": [], "category_id": hid}).status_code == 422
    assert client.post("/api/spending/assign", json={"keys": [key], "category_id": 9999}).status_code == 404
    hob = device(client.get("/api/spending/overview").json(), "Hobbies")
    assert hob["mtd"] == 8 and hob["status"] == "hihi" and hob["tags"][0]["assigned_by"] == "user"

    r = client.get(f"/api/spending/series?category={hid}&bucket=week&days=30")
    assert r.status_code == 200 and r.json()["label"] == "Hobbies"
    assert client.get(f"/api/spending/series?merchant={key}").json()["device"] == "Hobbies"
    assert client.get("/api/spending/series?category=abc").status_code == 422
    assert client.get("/api/spending/series?bucket=hour").status_code == 422
    assert client.get("/api/spending/series?category=9999").status_code == 404

    assert client.delete(f"/api/spending/categories/{hid}").json() == {"uncategorized": 1}
    assert client.delete(f"/api/spending/categories/{hid}").status_code == 404
    assert client.post("/api/spending/categories", json={"name": "Evil"},
                       headers={"origin": "https://evil.example"}).status_code == 403
