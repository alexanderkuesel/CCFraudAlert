"""Spend historian: card spending as SCADA-style tags.

SCADA            -> here
device           -> category (Groceries, Dining, ...), with a monthly budget as its setpoint
tag              -> merchant (normalised name)
tag value        -> spend in the home currency
HI / HIHI limit  -> 80% / 100% of the category's monthly budget (month to date)

Merchants are put in a category automatically from keywords the first time they're seen; anything the
user assigns wins and is never overwritten. Transactions acknowledged as fraud don't count as spending.

Fixed expenses (rent, transfers, cash: anything that never arrives as a card alert) are entered by hand
as recurring monthly amounts. Each one is a tag of its own ("manual:<id>") in the category you pick.
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fraudalert.anomaly.features import merchant_key
from fraudalert.models import Category, ManualExpense, MerchantTag, SyncState, Transaction

UNCATEGORIZED = "Uncategorized"
HI, HIHI = 0.8, 1.0  # fractions of the monthly budget
SPARK_MONTHS = 6
BUCKETS = ("day", "week", "month")

# Order matters: the first match wins, so specific keywords come before general ones
# ("uber eats" -> Dining before "uber" -> Transport).
DEFAULT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Dining", ["uber eats", "rappi", "restaurant", "restaurante", "soda ", "cafe", "coffee", "starbucks", "pizza",
                "mcdonald", "burger", "kfc", "taco", "sushi", "spoon", "panaderia", "bakery", "pollo", "bar "]),
    ("Groceries", ["automercado", "auto mercado", "pricesmart", "walmart", "masxmenos", "mas x menos", "pali",
                   "megasuper", "perimercado", "fresh market", "fast market", "supermercado", "super ", "grocery",
                   "whole foods", "supermarket", "mercado"]),
    ("Transport", ["uber", "didi", "gasolinera", "gas station", "shell", "chevron", "parqueo", "parking",
                   "peaje", "taxi", "fuel", "servicentro"]),
    ("Subscriptions", ["netflix", "spotify", "disney", "hbo", "max.com", "apple.com", "icloud", "youtube",
                       "prime video", "adobe", "microsoft", "openai", "chatgpt", "google"]),
    ("Travel", ["hotel", "booking", "airbnb", "airline", "avianca", "copa air", "united air", "american air",
                "expedia", "sansa", "hostel", "aeropuerto"]),
    ("Health", ["farmacia", "pharmacy", "fischel", "clinica", "hospital", "dental", "laboratorio", "gimnasio", "gym"]),
    ("Housing", ["alquiler", "rent ", "condominio", "mantenimiento", "hoa ", "mortgage", "hipoteca"]),
    ("Bills & utilities", ["kolbi", "claro", "liberty", "cnfl", "aya ", "ice ", "electric", "internet", "seguro",
                           "insurance", "telecom"]),
    ("Entertainment", ["cine", "cinepolis", "steam", "playstation", "xbox", "ticket", "eventbrite"]),
    ("Shopping", ["amazon", "ebay", "aliexpress", "shein", "temu", "best buy", "global-e", "gollo", "tienda",
                  "store", "mall", "electronics"]),
]
_SEEDED = "seeded_categories"
MANUAL = "manual:"  # tag-key prefix for fixed expenses


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s.casefold())
    return " " + " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split()) + " "


def guess_category(merchant: str) -> str | None:
    """Keyword match at a word start; keywords ending in a space must be whole words."""
    text = _fold(merchant)
    for name, keywords in DEFAULT_CATEGORIES:
        for kw in keywords:
            if re.search(r"(?<![a-z0-9])" + re.escape(kw.strip()) + (r"(?![a-z0-9])" if kw.endswith(" ") else ""), text):
                return name
    return None


# ---- categories & tags ----------------------------------------------------------------------------

def seed_categories(session: Session) -> None:
    """Create the default categories once (deleting one later sticks)."""
    if session.get(SyncState, _SEEDED):
        return
    existing = set(session.scalars(select(Category.name)))
    for i, (name, _) in enumerate(DEFAULT_CATEGORIES):
        if name not in existing:
            session.add(Category(name=name, sort=(i + 1) * 10))
    session.add(SyncState(key=_SEEDED, value="1"))
    session.flush()


def sync_tags(session: Session) -> int:
    """Create a tag for every merchant that doesn't have one yet, auto-categorised. Returns how many."""
    seed_categories(session)
    known = set(session.scalars(select(MerchantTag.merchant_key)))
    by_name = {c.name: c.id for c in session.scalars(select(Category))}
    added = 0
    for merchant in session.scalars(select(Transaction.merchant).distinct()):
        key = merchant_key(merchant or "")
        if not key or key in known:
            continue
        guess = guess_category(merchant or "")
        session.add(MerchantTag(merchant_key=key, category_id=by_name.get(guess), assigned_by="auto"))
        known.add(key)
        added += 1
    session.flush()
    return added


def _budget(value) -> Decimal | None:
    text = str(value if value is not None else "").replace(",", "").strip()
    if not text:
        return None
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ValueError("budget must be a number") from None
    if d < 0:
        raise ValueError("budget can't be negative")
    return d.quantize(Decimal("0.01"))


def create_category(session: Session, name: str, budget=None) -> Category:
    name = " ".join(str(name).split())[:64]
    if not name or name.casefold() == UNCATEGORIZED.casefold():
        raise ValueError("give the category a name (not “Uncategorized”)")
    if session.scalar(select(Category.id).where(func.lower(Category.name) == name.lower())):
        raise ValueError(f"a category called “{name}” already exists")
    top = max(session.scalars(select(Category.sort)), default=0)
    c = Category(name=name, budget_monthly=_budget(budget), sort=top + 10)
    session.add(c)
    session.flush()
    return c


def update_category(session: Session, category_id: int, name: str | None = None, budget="__keep__") -> Category:
    c = session.get(Category, category_id)
    if c is None:
        raise LookupError("no such category")
    if name is not None:
        new = " ".join(str(name).split())[:64]
        if not new or new.casefold() == UNCATEGORIZED.casefold():
            raise ValueError("give the category a name (not “Uncategorized”)")
        clash = session.scalar(select(Category.id).where(func.lower(Category.name) == new.lower(), Category.id != c.id))
        if clash:
            raise ValueError(f"a category called “{new}” already exists")
        c.name = new
    if budget != "__keep__":
        c.budget_monthly = _budget(budget)
    return c


def delete_category(session: Session, category_id: int) -> int:
    """Delete a category; its merchants become uncategorised (and stay user-owned). Returns how many."""
    c = session.get(Category, category_id)
    if c is None:
        raise LookupError("no such category")
    moved = 0
    for t in session.scalars(select(MerchantTag).where(MerchantTag.category_id == c.id)):
        t.category_id, t.assigned_by = None, "user"
        moved += 1
    for e in session.scalars(select(ManualExpense).where(ManualExpense.category_id == c.id)):
        e.category_id = None
        moved += 1
    session.delete(c)
    return moved


def assign(session: Session, keys: list[str], category_id: int | None) -> int:
    """User assignment of merchants (by key) to a category, or None for Uncategorized."""
    if category_id is not None and session.get(Category, category_id) is None:
        raise LookupError("no such category")
    manual = {k for k in keys if k.startswith(MANUAL)}
    for key in manual:
        e = session.get(ManualExpense, _manual_id(key))
        if e is None:
            raise LookupError(f"no such fixed expense: {key}")
        e.category_id = category_id
    keys = [k for k in keys if k not in manual]
    tags = {t.merchant_key: t for t in session.scalars(select(MerchantTag).where(MerchantTag.merchant_key.in_(keys)))}
    for key in keys:
        t = tags.get(key) or MerchantTag(merchant_key=key)
        t.category_id, t.assigned_by = category_id, "user"
        session.add(t)
    return len(keys) + len(manual)


# ---- fixed (manual) expenses ---------------------------------------------------------------------

def _manual_id(key: str) -> int:
    try:
        return int(key[len(MANUAL):])
    except ValueError:
        raise LookupError(f"no such fixed expense: {key}") from None


def _month(value, field: str) -> date | None:
    """"2026-03" or "2026-03-15" -> 2026-03-01; empty -> None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text + "-01" if len(text) == 7 else text).replace(day=1)
    except ValueError:
        raise ValueError(f"{field} must be a month like 2026-03") from None


def _expense_fields(session: Session, env, data: dict, current: ManualExpense | None = None) -> dict:
    out = {}
    if "name" in data or current is None:
        name = " ".join(str(data.get("name") or "").split())[:128]
        if not name:
            raise ValueError("give the expense a name")
        out["name"] = name
    if "amount" in data or current is None:
        try:
            amount = Decimal(str(data.get("amount") if data.get("amount") is not None else "").replace(",", "").strip())
        except InvalidOperation:
            raise ValueError("amount must be a number") from None
        if amount <= 0:
            raise ValueError("amount must be more than zero")
        out["amount"] = amount.quantize(Decimal("0.01"))
    if "currency" in data or current is None:
        cur = str(data.get("currency") or env.home_currency).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", cur) or env.fx.rate(cur) is None:
            raise ValueError(f"unknown currency {cur!r}; use a 3-letter code like USD or CRC")
        out["currency"] = cur
    if "category_id" in data:
        cid = data["category_id"]
        if cid in ("", None):
            out["category_id"] = None
        else:
            if session.get(Category, int(cid)) is None:
                raise LookupError("no such category")
            out["category_id"] = int(cid)
    if "day_of_month" in data or current is None:
        try:
            day = int(data.get("day_of_month") or 1)
        except (TypeError, ValueError):
            raise ValueError("day of month must be 1-31") from None
        if not 1 <= day <= 31:
            raise ValueError("day of month must be 1-31")
        out["day_of_month"] = day
    if "start_month" in data or current is None:
        start = _month(data.get("start_month"), "start month")
        out["start_month"] = start or _month_start(datetime.now(env.tz).date())
    if "end_month" in data:
        out["end_month"] = _month(data.get("end_month"), "end month")
    start = out.get("start_month", current.start_month if current else None)
    end = out.get("end_month", current.end_month if current else None)
    if end is not None and start is not None and end < start:
        raise ValueError("the end month is before the start month")
    if "note" in data:
        out["note"] = (str(data["note"] or "").strip() or None)
    return out


def create_expense(session: Session, env, data: dict) -> ManualExpense:
    e = ManualExpense(**_expense_fields(session, env, data))
    session.add(e)
    session.flush()
    return e


def update_expense(session: Session, env, expense_id: int, data: dict) -> ManualExpense:
    e = session.get(ManualExpense, expense_id)
    if e is None:
        raise LookupError("no such fixed expense")
    for k, v in _expense_fields(session, env, data, e).items():
        setattr(e, k, v)
    session.flush()
    return e


def delete_expense(session: Session, expense_id: int) -> None:
    e = session.get(ManualExpense, expense_id)
    if e is None:
        raise LookupError("no such fixed expense")
    session.delete(e)


def list_expenses(session: Session, env) -> list[dict]:
    rows = session.scalars(select(ManualExpense).order_by(ManualExpense.name, ManualExpense.id)).all()
    return [{
        "id": e.id, "key": f"{MANUAL}{e.id}", "name": e.name, "amount": float(e.amount), "currency": e.currency,
        "home_amount": env.fx.to_home(float(e.amount), e.currency), "category_id": e.category_id,
        "day_of_month": e.day_of_month, "start_month": e.start_month.isoformat()[:7],
        "end_month": e.end_month.isoformat()[:7] if e.end_month else None, "note": e.note,
    } for e in rows]


def _occurrences(e: ManualExpense, first: date, last: date) -> list[date]:
    """Dates the expense is booked between first and last (inclusive)."""
    out = []
    m = max(_month_start(first), e.start_month)
    end = min(_month_start(last), e.end_month) if e.end_month else _month_start(last)
    while m <= end:
        day = m.replace(day=min(e.day_of_month, (_add_months(m, 1) - m).days))
        if first <= day <= last:
            out.append(day)
        m = _add_months(m, 1)
    return out


def _category_map(session: Session) -> dict[str, int | None]:
    """Tag key -> category id, for merchants and fixed expenses."""
    out = {t.merchant_key: t.category_id for t in session.scalars(select(MerchantTag))}
    for eid, cid in session.execute(select(ManualExpense.id, ManualExpense.category_id)):
        out[f"{MANUAL}{eid}"] = cid
    return out


# ---- aggregation ----------------------------------------------------------------------------------

@dataclass
class Spend:
    key: str
    name: str
    local: datetime
    amount: float  # home currency


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def spends(session: Session, env, start: datetime, end: datetime) -> list[Spend]:
    rows = session.execute(
        select(Transaction.merchant, Transaction.occurred_at, Transaction.amount, Transaction.currency)
        .where(Transaction.occurred_at >= start, Transaction.occurred_at < end, Transaction.label_fraud.is_not(True))
    ).all()
    out = []
    for merchant, occurred_at, amount, currency in rows:
        value = env.fx.to_home(float(amount), currency)
        if value <= 0:
            continue
        out.append(Spend(merchant_key(merchant or "") or "(unknown)", merchant or "(unknown)",
                         _utc(occurred_at).astimezone(env.tz), value))
    # Fixed expenses are booked at noon local time on their day, up to `end` (nothing in the future).
    first, last = _utc(start).astimezone(env.tz).date(), (_utc(end).astimezone(env.tz) - timedelta(microseconds=1)).date()
    for e in session.scalars(select(ManualExpense)):
        value = env.fx.to_home(float(e.amount), e.currency)
        for day in _occurrences(e, first, last):
            local = datetime.combine(day, datetime.min.time(), tzinfo=env.tz).replace(hour=12)
            if start <= local < end:
                out.append(Spend(f"{MANUAL}{e.id}", e.name, local, value))
    return out


MIN_PACE_DAYS = 7  # a straight-line projection from the first few days of a month is noise


def _fixed_month(session: Session, env, month: date) -> dict[str, float]:
    """Tag key -> fixed-expense amount (home currency) booked in the whole of `month`."""
    last = _add_months(month, 1) - timedelta(days=1)
    out = {}
    for e in session.scalars(select(ManualExpense)):
        if _occurrences(e, month, last):
            out[f"{MANUAL}{e.id}"] = env.fx.to_home(float(e.amount), e.currency)
    return out


def _projection(mtd: float, fixed_to_date: float, fixed_month: float, day: int, days: int) -> float | None:
    """Month-end estimate: card (variable) spend at this month's pace, plus fixed expenses at face value."""
    if day < MIN_PACE_DAYS:
        return None
    return round((mtd - fixed_to_date) / day * days + fixed_month, 2)


def _status(value: float, budget: float | None) -> str:
    if not budget:
        return "none"
    return "hihi" if value >= budget * HIHI else "hi" if value >= budget * HI else "ok"


def overview(session: Session, env, now: datetime | None = None) -> dict:
    """The tag browser: every category ("device") with its merchants ("tags"), month-to-date values,
    last month, a 6-month sparkline, and budget status."""
    sync_tags(session)
    now = _utc(now or datetime.now(timezone.utc)).astimezone(env.tz)
    this_month = _month_start(now.date())
    spark_start = _add_months(this_month, -(SPARK_MONTHS - 1))
    start = datetime.combine(spark_start, datetime.min.time(), tzinfo=env.tz)
    rows = spends(session, env, start, _utc(now) + timedelta(seconds=1))
    months = [_add_months(spark_start, i) for i in range(SPARK_MONTHS)]
    idx = {m: i for i, m in enumerate(months)}

    tags = {t.merchant_key: t for t in session.scalars(select(MerchantTag))}
    catmap = _category_map(session)
    expense_names = {f"{MANUAL}{i}": n for i, n in session.execute(select(ManualExpense.id, ManualExpense.name))}
    cats = session.scalars(select(Category).order_by(Category.sort, Category.name)).all()
    names: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tag_spark: dict[str, list[float]] = defaultdict(lambda: [0.0] * SPARK_MONTHS)
    tag_count: dict[str, int] = defaultdict(int)
    for r in rows:
        names[r.key][r.name] += 1
        tag_spark[r.key][idx[_month_start(r.local.date())]] += r.amount
        if _month_start(r.local.date()) == this_month:
            tag_count[r.key] += 1

    def cat_of(key):
        return catmap.get(key)

    def tag_entry(key):
        sp = [round(v, 2) for v in tag_spark[key]]
        t = tags.get(key)
        return {"key": key, "name": max(names[key], key=names[key].get) if names[key] else expense_names.get(key, key),
                "mtd": sp[-1], "last_month": sp[-2] if len(sp) > 1 else 0.0, "spark": sp,
                "count": tag_count[key],
                "assigned_by": "manual" if key.startswith(MANUAL) else t.assigned_by if t else "auto"}

    day_of_month = now.day
    days_in_month = (_add_months(this_month, 1) - this_month).days
    fixed = _fixed_month(session, env, this_month)
    fixed_done = defaultdict(float)
    for r in rows:
        if r.key.startswith(MANUAL) and _month_start(r.local.date()) == this_month:
            fixed_done[r.key] += r.amount
    devices = []
    groups = [(c.id, c.name, float(c.budget_monthly) if c.budget_monthly is not None else None) for c in cats]
    groups.append((None, UNCATEGORIZED, None))
    for cid, name, budget in groups:
        keys = [k for k in set(tag_spark) | set(fixed) if cat_of(k) == cid]
        spark = [round(sum(tag_spark[k][i] for k in keys), 2) for i in range(SPARK_MONTHS)]
        mtd = spark[-1]
        if cid is None and not keys:
            continue
        devices.append({
            "id": cid, "name": name, "budget": budget, "mtd": mtd, "last_month": spark[-2], "spark": spark,
            "fixed": round(sum(fixed.get(k, 0) for k in keys), 2),
            "projected": _projection(mtd, sum(fixed_done[k] for k in keys), sum(fixed.get(k, 0) for k in keys),
                                     day_of_month, days_in_month),
            "pct": round(mtd / budget, 3) if budget else None, "status": _status(mtd, budget),
            "tags": sorted((tag_entry(k) for k in keys), key=lambda t: (-t["mtd"], -sum(t["spark"]), t["name"])),
        })
    total_spark = [round(sum(d["spark"][i] for d in devices), 2) for i in range(SPARK_MONTHS)]
    budgets = [d["budget"] for d in devices if d["budget"]]
    return {
        "currency": env.home_currency,
        "month": this_month.isoformat(), "months": [m.isoformat() for m in months],
        "day_of_month": day_of_month, "days_in_month": days_in_month,
        "limits": {"hi": HI, "hihi": HIHI},
        "total": {"mtd": total_spark[-1], "last_month": total_spark[-2], "spark": total_spark,
                  "fixed": round(sum(fixed.values()), 2),
                  "projected": _projection(total_spark[-1], sum(fixed_done.values()), sum(fixed.values()),
                                           day_of_month, days_in_month),
                  "budget": round(sum(budgets), 2) if budgets else None},
        "devices": devices,
    }


def series(session: Session, env, *, category: int | str | None = None, merchant: str | None = None,
           bucket: str = "day", days: int = 90, now: datetime | None = None) -> dict:
    """A historian trend: spend per day/week/month for one tag (merchant key), one device (category id,
    or "uncategorized"), or everything; plus this month's running total against the budget."""
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {', '.join(BUCKETS)}")
    sync_tags(session)
    now = _utc(now or datetime.now(timezone.utc)).astimezone(env.tz)
    today = now.date()
    if bucket == "month":
        first = _add_months(_month_start(today), -max(1, round(days / 30)) + 1)
    elif bucket == "week":
        first = today - timedelta(days=days - 1)
        first -= timedelta(days=first.weekday())  # Monday
    else:
        first = today - timedelta(days=days - 1)
    month_start = _month_start(today)
    start_day = min(first, month_start)
    rows = spends(session, env, datetime.combine(start_day, datetime.min.time(), tzinfo=env.tz),
                  _utc(now) + timedelta(seconds=1))

    tags = _category_map(session)
    budget, label = None, "All spending"
    if merchant is not None:
        rows = [r for r in rows if r.key == merchant]
        label = rows[0].name if rows else merchant
        cid = tags.get(merchant)
        parent = session.get(Category, cid) if cid else None
        label_device = parent.name if parent else UNCATEGORIZED
    elif category is not None:
        cid = None if category in ("uncategorized", None) else int(category)
        rows = [r for r in rows if tags.get(r.key) == cid]
        c = session.get(Category, cid) if cid is not None else None
        if cid is not None and c is None:
            raise LookupError("no such category")
        label = c.name if c else UNCATEGORIZED
        budget = float(c.budget_monthly) if c and c.budget_monthly is not None else None
        label_device = None
    else:
        budgets = [float(b) for b in session.scalars(select(Category.budget_monthly)) if b is not None]
        budget = sum(budgets) if budgets else None
        label_device = None

    def bucket_of(d: date) -> date:
        if bucket == "month":
            return _month_start(d)
        if bucket == "week":
            return d - timedelta(days=d.weekday())
        return d

    points: list[date] = []
    d = first
    while d <= today:
        points.append(d)
        d = _add_months(d, 1) if bucket == "month" else d + timedelta(days=7 if bucket == "week" else 1)
    values = {p: [0.0, 0] for p in points}
    for r in rows:
        b = bucket_of(r.local.date())
        if b in values:
            values[b][0] += r.amount
            values[b][1] += 1

    # month to date, cumulative by day, against the monthly budget
    days_in_month = (_add_months(month_start, 1) - month_start).days
    daily = [0.0] * days_in_month
    for r in rows:
        if r.local.date() >= month_start:
            daily[r.local.day - 1] += r.amount
    cumulative, running = [], 0.0
    for i in range(today.day):
        running += daily[i]
        cumulative.append(round(running, 2))
    keys = {r.key for r in rows} if merchant is None else {merchant}
    if merchant is None and category is not None:
        keys |= {k for k, c in tags.items() if c == cid}
    elif merchant is None:
        keys |= set(tags)
    fixed_all = _fixed_month(session, env, month_start)
    fixed_month = sum(v for k, v in fixed_all.items() if k in keys)
    fixed_done = sum(r.amount for r in rows if r.key.startswith(MANUAL) and r.local.date() >= month_start)
    return {
        "label": label, "device": label_device, "bucket": bucket, "currency": env.home_currency,
        "points": [{"start": p.isoformat(), "value": round(v[0], 2), "count": v[1]} for p, v in values.items()],
        "budget": budget,
        "mtd": {"month": month_start.isoformat(), "days_in_month": days_in_month, "cumulative": cumulative,
                "budget": budget, "status": _status(cumulative[-1] if cumulative else 0.0, budget),
                "fixed": round(fixed_month, 2),
                "projected": _projection(cumulative[-1] if cumulative else 0.0, fixed_done, fixed_month,
                                         today.day, days_in_month)},
    }

