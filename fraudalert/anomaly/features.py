"""Feature extraction: turns a transaction plus its history into a fixed-length numeric vector.

`FEATURE_NAMES` is the contract between ingestion and any model. Every transaction stores the
dict it was scored with (`Transaction.features`), so `fraudalert export-features` produces a
ready-made training set for a neural net later. If you add a feature, append it to the end and
bump `FEATURE_VERSION` so old rows can be recomputed or filtered out.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

FEATURE_VERSION = 1
FEATURE_NAMES = [
    "log_amount",
    "is_foreign",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "merchant_seen_log",  # log1p(# prior txns at this merchant)
    "amount_z_merchant",  # robust z-score of log amount vs this merchant's history
    "amount_z_global",  # robust z-score of log amount vs all history
    "hours_since_prev_log",
    "txns_last_24h",
    "history_size_log",
]


@dataclass
class TxnView:
    """Minimal transaction shape the feature code needs (works for ORM rows and tests)."""

    occurred_at: datetime  # tz-aware, already converted to the user's local time
    amount: float
    merchant: str
    is_foreign: bool


# Minimum spread (in log-amount units) for z-scores: with few, similar prices the MAD is tiny and a
# $2 difference would look like a 5-sigma event. 0.25 means z=1 is at least a ~28% price change.
MIN_LOG_SCALE = 0.25


def _robust_z(x: float, values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    scale = max(1.4826 * mad, MIN_LOG_SCALE)
    return max(-10.0, min(10.0, (x - med) / scale))


def merchant_key(merchant: str) -> str:
    return " ".join(merchant.casefold().split())


def compute_features(txn: TxnView, history: list[TxnView]) -> dict[str, float]:
    """`history` = earlier transactions (any order), not including `txn` itself."""
    log_amt = math.log1p(max(txn.amount, 0.0))
    hour = txn.occurred_at.hour + txn.occurred_at.minute / 60
    dow = txn.occurred_at.weekday()
    key = merchant_key(txn.merchant)
    same_merchant = [math.log1p(max(h.amount, 0)) for h in history if merchant_key(h.merchant) == key]
    all_amounts = [math.log1p(max(h.amount, 0)) for h in history]
    prior = [h.occurred_at for h in history if h.occurred_at <= txn.occurred_at]
    last = max(prior, default=None)
    hours_since = (txn.occurred_at - last).total_seconds() / 3600 if last else 24 * 30
    window = txn.occurred_at - timedelta(hours=24)
    return {
        "log_amount": log_amt,
        "is_foreign": float(txn.is_foreign),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "dow_sin": math.sin(2 * math.pi * dow / 7),
        "dow_cos": math.cos(2 * math.pi * dow / 7),
        "merchant_seen_log": math.log1p(len(same_merchant)),
        "amount_z_merchant": _robust_z(log_amt, same_merchant),
        "amount_z_global": _robust_z(log_amt, all_amounts),
        "hours_since_prev_log": math.log1p(max(hours_since, 0.0)),
        "txns_last_24h": float(sum(1 for t in prior if t >= window)),
        "history_size_log": math.log1p(len(history)),
    }


def to_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]
