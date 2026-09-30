"""Train, evaluate and store the Isolation Forest (`fraudalert train`, nightly in the worker, or the
Settings page)."""

import logging
import pickle
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from fraudalert.anomaly.baseline import BaselineDetector
from fraudalert.anomaly.features import FEATURE_VERSION
from fraudalert.anomaly.iforest import KEEP_MODELS, MIN_SAMPLES, PARAMS, TrainedForest, fit_forest
from fraudalert.models import AnomalyModel, Transaction

log = logging.getLogger(__name__)
ALARM_THRESHOLD = 0.97  # matches the built-in "Unusual pattern" rule


class NotEnoughData(Exception):
    pass


def _auc(pos: list[float], neg: list[float]) -> float | None:
    """Probability a random fraud scores above a random legit one (ties count half)."""
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def evaluate(model: TrainedForest, session: Session) -> dict:
    """Compare the forest with the baseline on what you've acknowledged (fraud vs legit), and check
    how many alarms the forest would raise. Note: legit rows were part of the training data, so treat
    this as a sanity check, not a benchmark."""
    rows = session.execute(
        select(Transaction.features, Transaction.label_fraud, Transaction.occurred_at)
        .where(Transaction.features.is_not(None))
    ).all()
    feats = [r.features for r in rows]
    forest = model.score_many(feats) if feats else []
    base = [BaselineDetector().score(f) for f in feats]
    fraud_i = [i for i, r in enumerate(rows) if r.label_fraud is True]
    legit_i = [i for i, r in enumerate(rows) if r.label_fraud is False]

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    week = datetime.now(timezone.utc) - timedelta(days=30)
    recent = [s for s, r in zip(forest, rows) if _aware(r.occurred_at) >= week]
    return {
        "labelled_fraud": len(fraud_i),
        "labelled_legit": len(legit_i),
        "auc_iforest": _auc([forest[i] for i in fraud_i], [forest[i] for i in legit_i]),
        "auc_baseline": _auc([base[i] or 0 for i in fraud_i], [base[i] or 0 for i in legit_i]),
        "mean_fraud_iforest": mean([forest[i] for i in fraud_i]),
        "mean_legit_iforest": mean([forest[i] for i in legit_i]),
        "alarm_threshold": ALARM_THRESHOLD,
        "alarms_30d_at_threshold": sum(1 for s in recent if s >= ALARM_THRESHOLD),
        "transactions_30d": len(recent),
    }


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def train_iforest(session: Session) -> AnomalyModel:
    """Fit on every scored transaction except those acknowledged as fraud; store and return the model."""
    feats = list(session.scalars(
        select(Transaction.features).where(Transaction.features.is_not(None), Transaction.label_fraud.is_not(True))
    ))
    if len(feats) < MIN_SAMPLES:
        raise NotEnoughData(f"need at least {MIN_SAMPLES} transactions to train, have {len(feats)}")
    model = fit_forest(feats)
    metrics = evaluate(model, session) | {"params": {k: v for k, v in PARAMS.items()}}
    row = AnomalyModel(kind="iforest", n_samples=len(feats), feature_version=FEATURE_VERSION,
                       blob=pickle.dumps(model), metrics=metrics)
    session.add(row)
    session.flush()
    old = session.scalars(select(AnomalyModel.id).where(AnomalyModel.kind == "iforest")
                          .order_by(AnomalyModel.id.desc()).offset(KEEP_MODELS)).all()
    if old:
        session.execute(delete(AnomalyModel).where(AnomalyModel.id.in_(old)))
    log.info("trained isolation forest on %d transactions: %s", len(feats), metrics)
    return row


def latest_model_info(session: Session) -> dict | None:
    row = session.execute(
        select(AnomalyModel.trained_at, AnomalyModel.n_samples, AnomalyModel.metrics)
        .where(AnomalyModel.kind == "iforest").order_by(AnomalyModel.id.desc()).limit(1)
    ).first()
    return dict(row._mapping) if row else None


def needs_retrain(session: Session, max_age: timedelta = timedelta(hours=24)) -> bool:
    """Retrain when there's no model yet (and enough data), or the model is old and new data arrived."""
    info = latest_model_info(session)
    count = session.scalar(select(func.count(Transaction.id)).where(Transaction.features.is_not(None))) or 0
    if count < MIN_SAMPLES:
        return False
    if info is None:
        return True
    trained = _aware(info["trained_at"])
    newer = session.scalar(select(func.count(Transaction.id)).where(Transaction.created_at > trained)) or 0
    return datetime.now(timezone.utc) - trained >= max_age and newer > 0
