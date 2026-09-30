"""Ingestion pipeline: email -> parsed transaction -> features -> anomaly score -> rules -> alerts."""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fraudalert.anomaly import AnomalyDetector, get_detector
from fraudalert.anomaly.features import TxnView, compute_features
from fraudalert.config import Settings, get_settings
from fraudalert.db import SYNC_LOCK_KEY, session_scope, try_advisory_lock
from fraudalert.ingest.message import EmailMessage, parse_rfc822
from fraudalert.ingest.parsers import ParseError, parse_email
from fraudalert.models import Alert, RawEmail, Rule, SyncState, Transaction
from fraudalert.notify import notify
from fraudalert.rules.engine import RuleSpec, describe, evaluate

log = logging.getLogger(__name__)

HISTORY_LIMIT = 1000
NOTIFY_MAX_AGE = timedelta(days=2)  # don't page on old mail during a historical backfill
_sync_lock = threading.Lock()


@dataclass
class SyncResult:
    fetched: int = 0
    parsed: int = 0
    failed: int = 0
    flagged: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = f"fetched {self.fetched}, parsed {self.parsed}, unparsed {self.failed}, flagged {self.flagged}"
        return s + (f" — errors: {'; '.join(self.errors)}" if self.errors else "")


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _view(txn: Transaction, tz: ZoneInfo) -> TxnView:
    return TxnView(
        occurred_at=_as_utc(txn.occurred_at).astimezone(tz),
        amount=float(txn.amount),
        merchant=txn.merchant or "",
        is_foreign=bool(txn.is_foreign),
    )


def transaction_context(txn: Transaction, tz: ZoneInfo) -> dict:
    """The dict rules are evaluated against (keys = rules.engine.FIELDS)."""
    local = _as_utc(txn.occurred_at).astimezone(tz)
    return {
        "amount": float(txn.amount),
        "currency": txn.currency,
        "merchant": txn.merchant or "",
        "card_last4": txn.card_last4,
        "is_foreign": bool(txn.is_foreign),
        "hour": local.hour,
        "weekday": local.weekday(),
        "anomaly_score": txn.anomaly_score,
    }


def load_rules(session: Session) -> list[RuleSpec]:
    rows = session.scalars(select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.id))
    return [RuleSpec(r.id, r.name, r.match, r.conditions, r.severity) for r in rows]


def score_transaction(session: Session, txn: Transaction, detector: AnomalyDetector, tz: ZoneInfo) -> None:
    q = select(Transaction).where(Transaction.occurred_at < txn.occurred_at)
    if txn.id is not None:
        q = q.where(Transaction.id != txn.id)
    history = session.scalars(q.order_by(Transaction.occurred_at.desc()).limit(HISTORY_LIMIT)).all()
    features = compute_features(_view(txn, tz), [_view(h, tz) for h in history])
    txn.features = features
    txn.anomaly_score = detector.score(features)
    txn.anomaly_model = detector.name


def apply_rules(session: Session, txn: Transaction, rules: list[RuleSpec], tz: ZoneInfo) -> list[Alert]:
    """Replace this transaction's rule alerts with a fresh evaluation. Returns the new alerts."""
    for stale in [a for a in txn.alerts if a.rule_id is not None]:
        txn.alerts.remove(stale)  # delete-orphan cascade removes the row
    ctx = transaction_context(txn, tz)
    new = []
    for rule in rules:
        if evaluate(rule, ctx):
            alert = Alert(rule_id=rule.id, reason=f"{rule.name}: {describe(rule)}", severity=rule.severity)
            txn.alerts.append(alert)
            new.append(alert)
    txn.flagged = bool(txn.alerts)
    session.flush()
    return new


def ingest_message(
    session: Session,
    msg: EmailMessage,
    settings: Settings,
    detector: AnomalyDetector,
    rules: list[RuleSpec],
    result: SyncResult,
) -> Transaction | None:
    if session.scalar(select(RawEmail.id).where(RawEmail.message_id == msg.message_id)):
        return None
    raw = RawEmail(
        message_id=msg.message_id,
        sender=msg.sender[:512],
        subject=msg.subject[:1024],
        received_at=msg.received_at,
        body=msg.body,
    )
    try:
        with session.begin_nested():  # savepoint: a duplicate must not poison the outer transaction
            session.add(raw)
            session.flush()
    except IntegrityError:
        # Another process stored this email between our check and the insert.
        log.info("skipping %s: already stored by another process", msg.message_id)
        return None
    result.fetched += 1
    return _parse_into_transaction(session, raw, settings, detector, rules, result)


def _parse_into_transaction(session, raw, settings, detector, rules, result) -> Transaction | None:
    msg = EmailMessage(raw.message_id, raw.sender, raw.subject, raw.received_at, raw.body)
    try:
        parsed, parser_name = parse_email(msg, settings.home_currency)
    except ParseError as exc:
        raw.parse_status, raw.parse_error = "failed", str(exc)
        result.failed += 1
        return None
    raw.parse_status, raw.parse_error, raw.parser_name = "parsed", None, parser_name
    result.parsed += 1

    tz = ZoneInfo(settings.timezone)
    txn = Transaction(
        email_id=raw.id,
        occurred_at=_as_utc(parsed.occurred_at),
        amount=parsed.amount,
        currency=parsed.currency,
        merchant=parsed.merchant,
        card_last4=parsed.card_last4,
        is_foreign=parsed.currency != settings.home_currency or parsed.foreign_hint,
    )
    session.add(txn)
    session.flush()
    score_transaction(session, txn, detector, tz)
    new_alerts = apply_rules(session, txn, rules, tz)
    if new_alerts:
        result.flagged += 1
        if datetime.now(timezone.utc) - txn.occurred_at <= NOTIFY_MAX_AGE:
            if notify(settings, txn, [a.reason for a in new_alerts]):
                for a in new_alerts:
                    a.notified = True
    return txn


def _get_state(session: Session, key: str) -> str | None:
    row = session.get(SyncState, key)
    return row.value if row else None


def _set_state(session: Session, key: str, value: str) -> None:
    row = session.get(SyncState, key)
    if row:
        row.value = value
    else:
        session.add(SyncState(key=key, value=value))


def sync_inbox(settings: Settings | None = None) -> SyncResult:
    """Pull new bank emails over IMAP and process them. Safe to call repeatedly and concurrently."""
    settings = settings or get_settings()
    result = SyncResult()
    if not _sync_lock.acquire(blocking=False):
        result.errors.append("a sync is already running")
        return result
    try:
        # The in-process lock covers the web UI's button; this one covers other processes
        # (the worker container vs. a manual `fraudalert sync`).
        with try_advisory_lock(SYNC_LOCK_KEY) as acquired:
            if not acquired:
                result.errors.append("a sync is already running in another process (e.g. the worker)")
                return result
            _sync(settings, result)
    finally:
        _sync_lock.release()
    log.info("sync: %s", result)
    return result


def _sync(settings: Settings, result: SyncResult) -> None:
    from fraudalert.ingest.imap_client import fetch_messages

    try:
        started = datetime.now(timezone.utc)
        with session_scope() as session:
            last = _get_state(session, "last_imap_sync")
        since = (
            datetime.fromisoformat(last) - timedelta(days=2)  # IMAP SINCE is date-granular; overlap a bit
            if last
            else started - timedelta(days=settings.lookback_days)
        )
        detector = get_detector(settings.detector)

        def seen(mid: str) -> bool:
            with session_scope() as s:
                return s.scalar(select(RawEmail.id).where(RawEmail.message_id == mid)) is not None

        for msg in fetch_messages(settings, since.date(), seen):
            # One transaction per email so a single bad message can't roll back the whole sync.
            try:
                with session_scope() as session:
                    ingest_message(session, msg, settings, detector, load_rules(session), result)
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to ingest %s", msg.message_id)
                result.errors.append(f"{msg.message_id}: {exc}")
        with session_scope() as session:
            _set_state(session, "last_imap_sync", started.isoformat())
    except Exception as exc:  # noqa: BLE001
        log.exception("sync failed")
        result.errors.append(str(exc))


def import_eml_files(paths: list[Path], settings: Settings | None = None) -> SyncResult:
    """Ingest saved .eml files (handy for testing parsers or backfilling from an export)."""
    settings = settings or get_settings()
    detector = get_detector(settings.detector)
    result = SyncResult()
    # Oldest first so each transaction is scored against the history that preceded it.
    msgs = sorted(
        (parse_rfc822(Path(p).read_bytes()) for p in paths),
        key=lambda m: _as_utc(m.received_at) if m.received_at else datetime.min.replace(tzinfo=timezone.utc),
    )
    for msg in msgs:
        with session_scope() as session:
            ingest_message(session, msg, settings, detector, load_rules(session), result)
    return result


def reevaluate_all(settings: Settings | None = None, reparse_failed: bool = False) -> SyncResult:
    """Re-score every transaction and re-apply the current rules (after rules or detector change).

    With `reparse_failed`, emails that previously failed to parse are retried first (after
    improving a parser). No notifications are sent for re-evaluated transactions.
    """
    settings = settings or get_settings()
    detector = get_detector(settings.detector)
    tz = ZoneInfo(settings.timezone)
    result = SyncResult()
    with session_scope() as session:
        rules = load_rules(session)
        if reparse_failed:
            quiet = settings.model_copy(update={"notify_webhook_url": ""})
            for raw in session.scalars(select(RawEmail).where(RawEmail.parse_status == "failed")).all():
                _parse_into_transaction(session, raw, quiet, detector, rules, result)
            result = SyncResult(parsed=result.parsed, failed=result.failed)
        for txn in session.scalars(select(Transaction).order_by(Transaction.occurred_at)).all():
            score_transaction(session, txn, detector, tz)
            if apply_rules(session, txn, rules, tz):
                result.flagged += 1
    return result


DEFAULT_RULES = [
    {
        "name": "Large or foreign purchase",
        "description": "Any purchase over 100 or charged in a non-home currency.",
        "match": "any",
        "conditions": [
            {"field": "amount", "op": "gt", "value": 100.0},
            {"field": "is_foreign", "op": "eq", "value": True},
        ],
        "severity": "high",
    },
]


def seed_default_rules(session: Session) -> None:
    if session.scalar(select(Rule.id).limit(1)) is None:
        for r in DEFAULT_RULES:
            session.add(Rule(**r))
