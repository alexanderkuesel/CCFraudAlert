"""Spend historian: card spending as SCADA-style tags.

SCADA            -> here
device           -> category (Groceries, Dining, ...), with a monthly budget as its setpoint
tag              -> merchant (normalised name)
tag value        -> spend in the home currency
HI / HIHI limit  -> 80% / 100% of the category's monthly budget (month to date)

Merchants are put in a category automatically from keywords the first time they're seen; anything the
user assigns wins and is never overwritten. Transactions acknowledged as fraud don't count as spending.
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
from fraudalert.models import Category, MerchantTag, SyncState, Transaction

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
    ("Bills & utilities", ["kolbi", "claro", "liberty", "cnfl", "aya ", "ice ", "electric", "internet", "seguro",
                           "insurance", "telecom"]),
    ("Entertainment", ["cine", "cinepolis", "steam", "playstation", "xbox", "ticket", "eventbrite"]),
    ("Shopping", ["amazon", "ebay", "aliexpress", "shein", "temu", "best buy", "global-e", "gollo", "tienda",
                  "store", "mall", "electronics"]),
]
_SEEDED = "seeded_categories"


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
    session.delete(c)
    return moved


def assign(session: Session, keys: list[str], category_id: int | None) -> int:
    """User assignment of merchants (by key) to a category, or None for Uncategorized."""
    if category_id is not None and session.get(Category, category_id) is None:
        raise LookupError("no such category")
    tags = {t.merchant_key: t for t in session.scalars(select(MerchantTag).where(MerchantTag.merchant_key.in_(keys)))}
    for key in keys:
        t = tags.get(key) or MerchantTag(merchant_key=key)
        t.category_id, t.assigned_by = category_id, "user"
        session.add(t)
    return len(keys)


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
    return out


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
        t = tags.get(key)
        return t.category_id if t else None

    def tag_entry(key):
        sp = [round(v, 2) for v in tag_spark[key]]
        t = tags.get(key)
        return {"key": key, "name": max(names[key], key=names[key].get) if names[key] else key,
                "mtd": sp[-1], "last_month": sp[-2] if len(sp) > 1 else 0.0, "spark": sp,
                "count": tag_count[key], "assigned_by": t.assigned_by if t else "auto"}

    day_of_month = now.day
    days_in_month = (_add_months(this_month, 1) - this_month).days
    devices = []
    groups = [(c.id, c.name, float(c.budget_monthly) if c.budget_monthly is not None else None) for c in cats]
    groups.append((None, UNCATEGORIZED, None))
    for cid, name, budget in groups:
        keys = [k for k in tag_spark if cat_of(k) == cid]
        spark = [round(sum(tag_spark[k][i] for k in keys), 2) for i in range(SPARK_MONTHS)]
        mtd = spark[-1]
        if cid is None and not keys:
            continue
        devices.append({
            "id": cid, "name": name, "budget": budget, "mtd": mtd, "last_month": spark[-2], "spark": spark,
            "projected": round(mtd / day_of_month * days_in_month, 2) if day_of_month else mtd,
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

    tags = {t.merchant_key: t.category_id for t in session.scalars(select(MerchantTag))}
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
    return {
        "label": label, "device": label_device, "bucket": bucket, "currency": env.home_currency,
        "points": [{"start": p.isoformat(), "value": round(v[0], 2), "count": v[1]} for p, v in values.items()],
        "budget": budget,
        "mtd": {"month": month_start.isoformat(), "days_in_month": days_in_month, "cumulative": cumulative,
                "budget": budget, "status": _status(cumulative[-1] if cumulative else 0.0, budget)},
    }

