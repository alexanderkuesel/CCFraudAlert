"""Card ↔ merchant spending network for the Network page.

Nodes are cards and merchants; an edge means a card was used at that merchant. Each merchant
carries the signals that make it worth a look: its review state (fraud / flagged / legit /
normal), whether it is new, whether it is foreign, and its highest anomaly score.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from fraudalert.anomaly.features import merchant_key
from fraudalert.models import Transaction

NEW_MERCHANT_DAYS = 14  # first-ever purchase at a merchant within this many days = "new"
UNUSUAL_SCORE = 0.7  # anomaly score at which an unreviewed transaction counts as flagged here

# Worst state wins when a merchant has several transactions.
STATE_ORDER = ["fraud", "flagged", "legit", "normal"]


@dataclass
class _Merchant:
    names: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    count: int = 0
    total: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    foreign: bool = False
    max_score: float | None = None
    states: set[str] = field(default_factory=set)
    cards: set[str] = field(default_factory=set)


def _state(t: Transaction) -> str:
    if t.label_fraud:
        return "fraud"
    if t.label_fraud is False:
        return "legit" if t.flagged else "normal"
    if t.flagged or (t.anomaly_score is not None and t.anomaly_score >= UNUSUAL_SCORE):
        return "flagged"
    return "normal"


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def build_network(session: Session, env, days: int | None, now: datetime | None = None) -> dict:
    """`env` is a pipeline.Env (currency conversion, foreign rules). `days=None` = all history."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None

    # First-ever purchase per merchant looks at all history, not just the window.
    first_ever: dict[str, datetime] = {}
    for merchant, occurred_at in session.execute(select(Transaction.merchant, Transaction.occurred_at)):
        key, ts = merchant_key(merchant or ""), _utc(occurred_at)
        if key not in first_ever or ts < first_ever[key]:
            first_ever[key] = ts

    q = select(Transaction)
    if since:
        q = q.where(Transaction.occurred_at >= since)
    merchants: dict[str, _Merchant] = defaultdict(_Merchant)
    edges: dict[tuple[str, str], dict] = {}
    card_totals: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": 0.0})

    for t in session.scalars(q):
        key = merchant_key(t.merchant or "") or "(unknown merchant)"
        card = t.card_last4 or "????"
        amount = env.fx.to_home(float(t.amount), t.currency)
        ts = _utc(t.occurred_at)
        m = merchants[key]
        m.names[t.merchant or "(unknown merchant)"] += 1
        m.count += 1
        m.total += amount
        m.first_seen = min(m.first_seen or ts, ts)
        m.last_seen = max(m.last_seen or ts, ts)
        m.foreign |= env.is_foreign(t)
        if t.anomaly_score is not None:
            m.max_score = max(m.max_score or 0.0, t.anomaly_score)
        m.states.add(_state(t))
        m.cards.add(card)
        e = edges.setdefault((card, key), {"count": 0, "total": 0.0, "flagged": 0})
        e["count"] += 1
        e["total"] += amount
        e["flagged"] += _state(t) in ("fraud", "flagged")
        card_totals[card]["count"] += 1
        card_totals[card]["total"] += amount

    new_cutoff = now - timedelta(days=NEW_MERCHANT_DAYS)
    nodes = [
        {"id": f"card:{c}", "kind": "card", "label": f"Card …{c}" if c != "????" else "Unknown card",
         "count": v["count"], "total": round(v["total"], 2)}
        for c, v in sorted(card_totals.items())
    ]
    for key, m in merchants.items():
        nodes.append({
            "id": f"m:{key}",
            "kind": "merchant",
            "label": max(m.names, key=m.names.get),
            "query": max(m.names, key=m.names.get),
            "count": m.count,
            "total": round(m.total, 2),
            "state": min(m.states, key=STATE_ORDER.index),
            "new": first_ever.get(key, m.first_seen) >= new_cutoff,
            "foreign": m.foreign,
            "max_score": None if m.max_score is None else round(m.max_score, 2),
            "first_seen": first_ever.get(key, m.first_seen).isoformat(),
            "last_seen": m.last_seen.isoformat(),
            "cards": len(m.cards),
        })
    return {
        "home_currency": env.home_currency,
        "days": days,
        "new_merchant_days": NEW_MERCHANT_DAYS,
        "nodes": nodes,
        "edges": [
            {"source": f"card:{c}", "target": f"m:{k}", "count": v["count"], "total": round(v["total"], 2),
             "flagged": v["flagged"]}
            for (c, k), v in edges.items()
        ],
    }
