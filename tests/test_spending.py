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


def test_fixed_expenses_book_monthly_and_count_toward_budgets(db):
    from fraudalert.models import ManualExpense

    with db.session_scope() as s:
        spending.seed_categories(s)
        housing = s.scalar(select(Category).where(Category.name == "Housing"))
        housing.budget_monthly = 1000
        e = spending.create_expense(s, env(s), {"name": " Rent ", "amount": "900", "category_id": housing.id,
                                                "day_of_month": 31, "start_month": "2026-04"})
        assert (e.name, e.currency, e.start_month.isoformat(), e.end_month) == ("Rent", "USD", "2026-04-01", None)
        spending.create_expense(s, env(s), {"name": "Nanny", "amount": "100000", "currency": "crc",
                                            "day_of_month": 20, "start_month": "2026-01", "end_month": "2026-05"})
        for bad, msg in [({"name": "", "amount": 1}, "name"), ({"name": "x", "amount": "0"}, "more than zero"),
                         ({"name": "x", "amount": "1", "currency": "XYZ"}, "currency"),
                         ({"name": "x", "amount": "1", "day_of_month": 32}, "1-31"),
                         ({"name": "x", "amount": "1", "start_month": "2026-05", "end_month": "2026-04"}, "before"),
                         ({"name": "x", "amount": "1", "start_month": "May"}, "month like")]:
            with pytest.raises(ValueError, match=msg):
                spending.create_expense(s, env(s), bad)
        with pytest.raises(LookupError):
            spending.create_expense(s, env(s), {"name": "x", "amount": 1, "category_id": 9999})
        add(s, "ALQUILER BODEGA", 50, NOW - timedelta(days=1))  # card spend, auto-tagged to Housing

    with db.session_scope() as s:
        rent = s.scalar(select(ManualExpense).where(ManualExpense.name == "Rent"))
        # April has 30 days, so "day 31" is booked on Apr 30; June 30 is still in the future on Jun 15
        month = spending.series(s, env(s), merchant=f"manual:{rent.id}", bucket="month", days=120, now=NOW)
        assert [(p["start"], p["value"]) for p in month["points"]] == [
            ("2026-03-01", 0), ("2026-04-01", 900), ("2026-05-01", 900), ("2026-06-01", 0)]
        assert month["label"] == "Rent" and month["device"] == "Housing"
        day = spending.series(s, env(s), merchant=f"manual:{rent.id}", bucket="day", days=60, now=NOW)
        assert {p["start"] for p in day["points"] if p["value"]} == {"2026-04-30", "2026-05-31"}

        ov = spending.overview(s, env(s), now=NOW)
        h = device(ov, "Housing")
        # rent isn't due until the 30th: not spent yet, but projected at face value
        assert h["mtd"] == 50 and h["fixed"] == 900 and h["projected"] == 50 / 15 * 30 + 900
        assert {t["name"]: t["assigned_by"] for t in h["tags"]} == {"ALQUILER BODEGA": "auto", "Rent": "manual"}
        u = device(ov, spending.UNCATEGORIZED)
        nanny = next(t for t in u["tags"] if t["name"] == "Nanny")
        assert nanny["mtd"] == 0 and nanny["spark"][1:5] == [200.0] * 4  # Jan-May at the built-in 0.0020 CRC rate, then ended
        assert ov["total"]["fixed"] == 900  # Nanny ended in May

        cat = spending.series(s, env(s), category=h["id"], bucket="day", days=30, now=NOW)
        assert cat["mtd"]["fixed"] == 900 and cat["mtd"]["projected"] == h["projected"]

        # moving a fixed expense to another category goes through the same assign call
        dining = s.scalar(select(Category.id).where(Category.name == "Dining"))
        assert spending.assign(s, [f"manual:{rent.id}"], dining) == 1
        assert rent.category_id == dining
        spending.update_expense(s, env(s), rent.id, {"amount": "950", "end_month": "2026-05"})
        assert float(rent.amount) == 950 and rent.end_month.isoformat() == "2026-05-01"
        with pytest.raises(ValueError, match="before"):
            spending.update_expense(s, env(s), rent.id, {"end_month": "2026-03"})
        spending.delete_category(s, dining)
        s.flush()
        assert rent.category_id is None


def test_fixed_expenses_api(db):
    client = TestClient(create_app(init=False))
    r = client.post("/api/spending/expenses", json={"name": "Rent", "amount": "1200", "day_of_month": "1"})
    assert r.status_code == 201
    eid = r.json()["id"]
    assert client.post("/api/spending/expenses", json={"name": "x", "amount": "abc"}).status_code == 422
    assert client.post("/api/spending/expenses", json={"name": "x", "amount": "1", "category_id": "abc"}).status_code == 422
    [e] = client.get("/api/spending/expenses").json()
    assert e["key"] == f"manual:{eid}" and e["currency"] == "USD" and e["home_amount"] == 1200 and e["end_month"] is None
    assert client.patch(f"/api/spending/expenses/{eid}", json={"note": "lease to 2027"}).status_code == 200
    assert client.get("/api/spending/expenses").json()[0]["note"] == "lease to 2027"
    ov = client.get("/api/spending/overview").json()
    assert ov["total"]["mtd"] == 1200 and device(ov, spending.UNCATEGORIZED)["tags"][0]["assigned_by"] == "manual"
    assert client.patch("/api/spending/expenses/9999", json={"note": "x"}).status_code == 404
    assert client.delete(f"/api/spending/expenses/{eid}").status_code == 204
    assert client.delete(f"/api/spending/expenses/{eid}").status_code == 404
    assert client.get("/api/spending/overview").json()["total"]["mtd"] == 0


def test_expected_path_forecast_and_history(db):
    def on(month, day):
        return datetime(2026, month, day, 16, tzinfo=timezone.utc)

    with db.session_scope() as s:
        spending.seed_categories(s)
        for m in (3, 4, 5):  # three complete months, 60 each: 30 on the 1st and 30 on the 20th
            add(s, "PIZZA HUT", 30, on(m, 1))
            add(s, "PIZZA HUT", 30, on(m, 20))
        add(s, "PIZZA HUT", 999, on(5, 25), fraud=True)  # never part of "usual"
        add(s, "PIZZA HUT", 10, on(6, 2))
        spending.create_expense(s, env(s), {"name": "Rent", "amount": 100, "day_of_month": 20, "start_month": "2026-06"})

    with db.session_scope() as s:
        cur = spending.series(s, env(s), now=NOW)
        m = cur["mtd"]
        assert m["current"] and m["basis"] == ["2026-05", "2026-04", "2026-03"]
        assert len(m["expected"]) == 30 and m["expected"][0] == 30 and m["expected"][-1] == 160  # 60 usual + rent
        assert m["expected"][14] == 30  # by the 15th you've usually spent 30; rent isn't due yet
        # forecast: today's actual + the rest of the usual path + the rent still to come
        assert m["cumulative"][-1] == 10 and m["projected"] == 10 + 30 + 100
        hist = {h["month"]: h for h in cur["history"]}
        assert hist["2026-03"]["expected"] is None and hist["2026-03"]["actual"] == 60  # no history before March
        assert hist["2026-02"]["actual"] == 0 and hist["2026-02"]["expected"] is None
        assert hist["2026-04"]["expected"] == 60 and hist["2026-05"]["expected"] == 60
        assert hist["2026-06"]["current"] and hist["2026-06"]["actual"] == 10 and hist["2026-06"]["projected"] == 140
        assert list(hist)[-1] == "2026-06" and len(hist) == spending.HISTORY_MONTHS + 1

        may = spending.series(s, env(s), month="2026-05", now=NOW)["mtd"]
        assert not may["current"] and len(may["cumulative"]) == 31 and may["cumulative"][-1] == 60
        assert may["projected"] is None and may["basis"] == ["2026-04", "2026-03"]
        with pytest.raises(ValueError, match="hasn't happened"):
            spending.series(s, env(s), month="2026-07", now=NOW)

        ov = spending.overview(s, env(s), now=NOW)
        assert ov["total"]["projected"] == 140 and ov["total"]["expected"] == 30
        dining = device(ov, "Dining")
        assert dining["projected"] == 40 and dining["expected"] == 30

    client = TestClient(create_app(init=False))
    assert client.get("/api/spending/series?month=2026-05").json()["mtd"]["month"] == "2026-05-01"
    assert client.get("/api/spending/series?month=nope").status_code == 422
