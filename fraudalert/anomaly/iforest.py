"""Isolation Forest anomaly detector.

Unsupervised: it learns what *your* normal spending looks like from stored transactions (anything you
acknowledged as fraud is left out) and isolates points that don't fit. Raw forest scores are turned
into a percentile against the training data, so a score of 0.98 reads as "more unusual than 98% of
your history".

Until a model has been trained on at least MIN_SAMPLES transactions, scoring falls back to the
statistical baseline, so switching FRAUDALERT_DETECTOR=iforest is always safe.
"""

import bisect
import logging
import pickle
from datetime import datetime, timezone

from sqlalchemy import select

from fraudalert.anomaly.base import AnomalyDetector, register
from fraudalert.anomaly.baseline import BaselineDetector
from fraudalert.anomaly.features import FEATURE_NAMES, FEATURE_VERSION

log = logging.getLogger(__name__)

MIN_SAMPLES = 50
KEEP_MODELS = 5
# history_size grows with every transaction, so the newest one would always look "out of range".
MODEL_FEATURES = [f for f in FEATURE_NAMES if f != "history_size_log"]
PARAMS = {"n_estimators": 200, "max_samples": "auto", "contamination": "auto", "random_state": 42}


def _vector(features: dict) -> list[float]:
    return [float(features.get(name, 0.0)) for name in MODEL_FEATURES]


class TrainedForest:
    """The fitted forest plus the sorted training scores used to turn a raw score into a percentile."""

    def __init__(self, forest, reference: list[float]):
        self.forest = forest
        self.reference = reference  # sorted raw anomaly scores of the training data (higher = odder)

    def raw(self, rows: list[dict]) -> list[float]:
        return [-s for s in self.forest.score_samples([_vector(r) for r in rows])]

    def percentile(self, raw: float) -> float:
        return round(bisect.bisect_left(self.reference, raw) / len(self.reference), 4)

    def score_many(self, rows: list[dict]) -> list[float]:
        return [self.percentile(r) for r in self.raw(rows)]


def fit_forest(rows: list[dict]) -> TrainedForest:
    from sklearn.ensemble import IsolationForest

    forest = IsolationForest(**PARAMS).fit([_vector(r) for r in rows])
    model = TrainedForest(forest, [])
    model.reference = sorted(model.raw(rows))
    return model


# Process-wide cache of the newest stored model, refreshed when a newer one appears in the DB.
_cache: dict = {"id": None, "model": None, "trained_at": None}


def load_latest(session=None) -> tuple[TrainedForest | None, datetime | None]:
    from fraudalert.db import session_scope
    from fraudalert.models import AnomalyModel

    def _load(s):
        row = s.execute(
            select(AnomalyModel.id, AnomalyModel.trained_at, AnomalyModel.feature_version)
            .where(AnomalyModel.kind == "iforest").order_by(AnomalyModel.id.desc()).limit(1)
        ).first()
        if row is None or row.feature_version != FEATURE_VERSION:
            _cache.update(id=None, model=None, trained_at=None)
        elif row.id != _cache["id"]:
            blob = s.scalar(select(AnomalyModel.blob).where(AnomalyModel.id == row.id))
            # The blob was written by this app into its own database (see train_iforest).
            _cache.update(id=row.id, model=pickle.loads(blob), trained_at=row.trained_at)
        return _cache["model"], _cache["trained_at"]

    if session is not None:
        return _load(session)
    with session_scope() as s:
        return _load(s)


@register("iforest")
class IsolationForestDetector(AnomalyDetector):
    name = "iforest-v1"

    def __init__(self, session=None) -> None:
        self._baseline = BaselineDetector()
        try:
            self.model, trained_at = load_latest(session)
        except Exception:  # noqa: BLE001 - never let a bad model stop ingestion
            log.exception("could not load the anomaly model; using the baseline")
            self.model, trained_at = None, None
        if self.model is None:
            self.name = BaselineDetector.name  # record honestly which model produced the score
        else:
            stamp = trained_at.astimezone(timezone.utc) if trained_at.tzinfo else trained_at
            self.name = f"iforest-v1@{stamp:%Y-%m-%d}"

    def score(self, features: dict[str, float]) -> float | None:
        if self.model is None:
            return self._baseline.score(features)
        return self.model.score_many([features])[0]
