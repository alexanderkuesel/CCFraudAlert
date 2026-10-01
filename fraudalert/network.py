"""Card ↔ merchant spending network for the Network page.

Nodes are cards and merchants; an edge means a card was used at that merchant. Each merchant
carries the signals that make it worth a look: its alarm state (fraud, an unacknowledged alarm of
priority 1-3, acknowledged legit, or normal), whether it is new, whether it is foreign, and its
highest anomaly score.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fraudalert.anomaly.features import merchant_key
from fraudalert.models import Transaction

NEW_MERCHANT_DAYS = 14  # first-ever purchase at a merchant within this many days = "new"

# Worst state wins when a merchant has several transactions. p1-p3 = an unacknowledged alarm of that
# ISA-18.2 priority; legit = alarms acknowledged as legit; normal = no alarm.
STATE_ORDER = ["fraud", "p1", "p2", "p3", "legit", "normal"]
_PRIORITY = {"high": 1, "medium": 2, "low": 3}


@dataclass
class _Merchant:
    names: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    count: int = 0
    total: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    foreign: bool = False
    max_score: float | None = None
    scores: list[float] = field(default_factory=list)
    reasons: list | None = None  # "why unusual" of the highest-scoring transaction
    states: set[str] = field(default_factory=set)
    cards: set[str] = field(default_factory=set)


def _state(t: Transaction) -> str:
    if t.label_fraud:
        return "fraud"
    if t.label_fraud is False:
        return "legit" if t.flagged else "normal"
    if t.flagged and t.alerts:
        return f"p{min(_PRIORITY.get(a.severity, 3) for a in t.alerts)}"
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

    q = select(Transaction).options(selectinload(Transaction.alerts))
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
            m.scores.append(round(t.anomaly_score, 3))
            if m.max_score is None or t.anomaly_score >= m.max_score:
                m.max_score = t.anomaly_score
                m.reasons = t.anomaly_reasons or m.reasons
        m.states.add(_state(t))
        m.cards.add(card)
        e = edges.setdefault((card, key), {"count": 0, "total": 0.0, "flagged": 0})
        e["count"] += 1
        e["total"] += amount
        e["flagged"] += _state(t) in ("fraud", "p1", "p2", "p3")
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
            "max_score": None if m.max_score is None else round(m.max_score, 3),
            "scores": sorted(m.scores, reverse=True),
            "reasons": [r["text"] for r in (m.reasons or [])],
            "first_seen": first_ever.get(key, m.first_seen).isoformat(),
            "last_seen": m.last_seen.isoformat(),
            "cards": len(m.cards),
        })
    return {
        "home_currency": env.home_currency,
        "anomaly": anomaly_info(session),
        "days": days,
        "new_merchant_days": NEW_MERCHANT_DAYS,
        "nodes": nodes,
        "edges": [
            {"source": f"card:{c}", "target": f"m:{k}", "count": v["count"], "total": round(v["total"], 2),
             "flagged": v["flagged"]}
            for (c, k), v in edges.items()
        ],
    }


DEFAULT_LIMIT = 0.97


def anomaly_info(session: Session) -> dict:
    """Which model is scoring, and the default limit for the map's slider: the threshold of the enabled
    rule that alarms on anomaly_score (the built-in "Unusual pattern" rule), else 0.97."""
    from fraudalert.models import Rule

    limit = None
    for rule in session.scalars(select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.id)):
        for c in rule.conditions or []:
            if c.get("field") == "anomaly_score" and c.get("op") in ("gte", "gt"):
                limit = float(c["value"]) if limit is None else min(limit, float(c["value"]))
    model = session.scalar(select(Transaction.anomaly_model).where(Transaction.anomaly_model.is_not(None))
                           .order_by(Transaction.id.desc()).limit(1))
    return {"limit": limit if limit is not None else DEFAULT_LIMIT, "model": model or "baseline-v1",
            "isolation_forest": bool(model and model.startswith("iforest"))}
