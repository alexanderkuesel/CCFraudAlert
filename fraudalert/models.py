from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fraudalert.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RawEmail(Base):
    """Every email pulled from the inbox, kept so parsing can be re-run and failures inspected."""

    __tablename__ = "raw_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(1024))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body: Mapped[str] = mapped_column(Text)
    # parsed | failed | ignored
    parse_status: Mapped[str] = mapped_column(String(16), default="failed", index=True)
    parse_error: Mapped[str | None] = mapped_column(Text)
    parser_name: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    transaction: Mapped["Transaction | None"] = relationship(back_populates="email")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_occurred_at", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("raw_emails.id", ondelete="SET NULL"), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    merchant: Mapped[str] = mapped_column(String(512), default="")
    card_last4: Mapped[str | None] = mapped_column(String(4))
    auth_code: Mapped[str | None] = mapped_column(String(32))  # bank's authorization code, quote it when reporting
    reference: Mapped[str | None] = mapped_column(String(64))  # bank's reference number, if the email has one
    is_foreign: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), default="email")

    # Anomaly detection. `features` is the exact vector the detector saw, so the table doubles
    # as a training set for a future model; `anomaly_model` records which model produced the score.
    features: Mapped[dict | None] = mapped_column(JSON)
    anomaly_score: Mapped[float | None] = mapped_column(Float)
    anomaly_model: Mapped[str | None] = mapped_column(String(64))

    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # User feedback: None = unreviewed, True = confirmed fraud, False = legit. Future training labels.
    label_fraud: Mapped[bool | None] = mapped_column(Boolean)
    comment: Mapped[str | None] = mapped_column(Text)  # your review note
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    email: Mapped[RawEmail | None] = relationship(back_populates="transaction")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")


class Rule(Base):
    """A user-defined rule. `conditions` is a list of {field, op, value}; `match` is "all" or "any"."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    match: Mapped[str] = mapped_column(String(8), default="all")
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    alerts: Mapped[list["Alert"]] = relationship(back_populates="rule", cascade="all, delete-orphan")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    # Null rule_id = raised by the anomaly detector rather than a rule.
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    transaction: Mapped[Transaction] = relationship(back_populates="alerts")
    rule: Mapped[Rule | None] = relationship(back_populates="alerts")


class SyncState(Base):
    """Key/value bookkeeping (e.g. last successful inbox sync)."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AnomalyModel(Base):
    """A trained anomaly model (e.g. an Isolation Forest). Stored in the database so the web and
    worker containers share it and it survives restarts. Only the newest few are kept."""

    __tablename__ = "anomaly_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # "iforest"
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    n_samples: Mapped[int] = mapped_column(Integer)
    feature_version: Mapped[int] = mapped_column(Integer)
    blob: Mapped[bytes] = mapped_column(LargeBinary)  # pickled model + reference score distribution
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
