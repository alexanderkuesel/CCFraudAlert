"""Alarm-summary filters, parsed from the query string.

The same Filters object drives the page and the "select all N matching" bulk action, so a bulk edit
always hits exactly the rows the user was looking at.
"""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urlencode

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from fraudalert.models import Alert, Transaction

VIEWS = {"unack": "Unacknowledged", "alarms": "All alarms", "journal": "Journal"}
PRIORITY_RANK = {"high": 1, "medium": 2, "low": 3}
PRIORITIES = {"1": "High", "2": "Medium", "3": "Low", "none": "No alarm"}
STATES = {"unack": "Unacknowledged", "fraud": "Fraud", "legit": "Legit", "none": "No alarm"}


def txn_priority(t: Transaction) -> int | None:
    """Highest (numerically lowest) priority among a transaction's alarms."""
    ranks = [PRIORITY_RANK.get(a.severity, 3) for a in t.alerts]
    return min(ranks) if ranks else None


def txn_state(t: Transaction) -> str:
    if t.label_fraud:
        return "fraud"
    if t.label_fraud is False:
        return "legit"
    return "unack" if t.flagged else "none"


def _float(value: str, name: str, errors: list[str]) -> float | None:
    if not value.strip():
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        errors.append(f"{name} must be a number")
        return None


def _date(value: str, name: str, errors: list[str]) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        errors.append(f"{name} must be a date (YYYY-MM-DD)")
        return None


@dataclass
class Filters:
    view: str = "unack"
    q: str = ""  # merchant / currency contains
    pri: set[str] = field(default_factory=set)  # {"1", "2", "3", "none"}
    state: set[str] = field(default_factory=set)  # {"unack", "fraud", "legit", "none"}
    date_from: date | None = None  # local dates, inclusive
    date_to: date | None = None
    amount_min: float | None = None  # home currency
    amount_max: float | None = None
    anomaly_min: float | None = None
    card: str = ""
    rule: int | None = None
    foreign: str = ""  # "" | "yes" | "no"
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_params(cls, params) -> "Filters":
        """`params` is a Starlette QueryParams / FormData or a parse_qs dict (anything with getlist)."""
        get = (lambda k: params.get(k, "")) if hasattr(params, "getlist") else (lambda k: (params.get(k) or [""])[0])
        getlist = params.getlist if hasattr(params, "getlist") else (lambda k: params.get(k, []))
        errors: list[str] = []
        view = get("view")
        if get("flagged") in ("true", "1"):  # old links
            view = "alarms"
        rule = get("rule").strip()
        return cls(
            view=view if view in VIEWS else "unack",
            q=get("q").strip(),
            pri={p for p in getlist("pri") if p in PRIORITIES},
            state={s for s in getlist("state") if s in STATES},
            date_from=_date(get("from"), "From", errors),
            date_to=_date(get("to"), "To", errors),
            amount_min=_float(get("amin"), "Min amount", errors),
            amount_max=_float(get("amax"), "Max amount", errors),
            anomaly_min=_float(get("anom"), "Min anomaly", errors),
            card=get("card").strip()[:4],
            rule=int(rule) if rule.isdigit() else None,
            foreign=get("foreign") if get("foreign") in ("yes", "no") else "",
            errors=errors,
        )

    @classmethod
    def from_query_string(cls, qs: str) -> "Filters":
        return cls.from_params(parse_qs(qs))

    def items(self, include_view: bool = True) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = [("view", self.view)] if include_view else []
        if self.q:
            out.append(("q", self.q))
        out += [("pri", p) for p in sorted(self.pri)]
        out += [("state", s) for s in sorted(self.state)]
        for key, value in (("from", self.date_from), ("to", self.date_to), ("amin", self.amount_min),
                           ("amax", self.amount_max), ("anom", self.anomaly_min)):
            if value is not None:
                out.append((key, value.isoformat() if isinstance(value, date) else f"{value:g}"))
        if self.card:
            out.append(("card", self.card))
        if self.rule is not None:
            out.append(("rule", str(self.rule)))
        if self.foreign:
            out.append(("foreign", self.foreign))
        return out

    def query_string(self, **overrides) -> str:
        return urlencode((replace(self, **overrides) if overrides else self).items())

    def chips(self, rule_names: dict[int, str], currency: str) -> list[tuple[str, str]]:
        """(label, URL with that filter removed) for each active filter."""
        chips = []
        if self.q:
            chips.append((f"Merchant/currency: “{self.q}”", self.query_string(q="")))
        if self.pri:
            chips.append(("Priority: " + ", ".join(PRIORITIES[p] for p in sorted(self.pri)), self.query_string(pri=set())))
        if self.state:
            chips.append(("State: " + ", ".join(STATES[s] for s in sorted(self.state)), self.query_string(state=set())))
        if self.date_from or self.date_to:
            chips.append((f"Date: {self.date_from or '…'} → {self.date_to or '…'}",
                          self.query_string(date_from=None, date_to=None)))
        if self.amount_min is not None or self.amount_max is not None:
            lo = f"{self.amount_min:g}" if self.amount_min is not None else "…"
            hi = f"{self.amount_max:g}" if self.amount_max is not None else "…"
            chips.append((f"Amount: {lo} – {hi} {currency}", self.query_string(amount_min=None, amount_max=None)))
        if self.anomaly_min is not None:
            chips.append((f"Anomaly ≥ {self.anomaly_min:g}", self.query_string(anomaly_min=None)))
        if self.card:
            chips.append((f"Card …{self.card}", self.query_string(card="")))
        if self.rule is not None:
            chips.append((f"Alarm: {rule_names.get(self.rule, f'rule #{self.rule}')}", self.query_string(rule=None)))
        if self.foreign:
            chips.append(("Foreign only" if self.foreign == "yes" else "Not foreign", self.query_string(foreign="")))
        return chips

    @property
    def active(self) -> bool:
        return bool(self.items(include_view=False))

    def apply(self, session: Session, env) -> list[Transaction]:
        """Matching transactions, sorted for display. `env` is a pipeline.Env (tz, fx, foreign rules)."""
        stmt = select(Transaction).options(selectinload(Transaction.alerts).selectinload(Alert.rule))
        if self.view == "unack":
            stmt = stmt.where(Transaction.flagged.is_(True), Transaction.label_fraud.is_(None))
        elif self.view == "alarms":
            stmt = stmt.where(Transaction.flagged.is_(True))
        if self.q:
            like = f"%{self.q}%"
            stmt = stmt.where(or_(Transaction.merchant.ilike(like), Transaction.currency.ilike(like)))
        if self.date_from:
            stmt = stmt.where(Transaction.occurred_at >= _utc_start(self.date_from, env.tz))
        if self.date_to:
            stmt = stmt.where(Transaction.occurred_at < _utc_start(self.date_to + timedelta(days=1), env.tz))
        if self.anomaly_min is not None:
            stmt = stmt.where(Transaction.anomaly_score >= self.anomaly_min)
        if self.card:
            stmt = stmt.where(Transaction.card_last4 == self.card)
        if self.rule is not None:
            stmt = stmt.where(Transaction.alerts.any(Alert.rule_id == self.rule))
        rows = session.scalars(stmt).all()

        def keep(t: Transaction) -> bool:
            if self.pri:
                p = txn_priority(t)
                if (str(p) if p else "none") not in self.pri:
                    return False
            if self.state and txn_state(t) not in self.state:
                return False
            if self.amount_min is not None or self.amount_max is not None:
                amount = env.fx.to_home(float(t.amount), t.currency)
                if self.amount_min is not None and amount < self.amount_min:
                    return False
                if self.amount_max is not None and amount > self.amount_max:
                    return False
            if self.foreign and env.is_foreign(t) != (self.foreign == "yes"):
                return False
            return True

        rows = [t for t in rows if keep(t)]
        if self.view == "journal":
            rows.sort(key=lambda t: t.occurred_at, reverse=True)
        else:  # alarm lists: unacknowledged first, then priority, then newest
            rows.sort(key=lambda t: (t.label_fraud is not None, txn_priority(t) or 9, -t.occurred_at.timestamp()))
        return rows


def _utc_start(d: date, tz) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)
