from datetime import datetime, timedelta, timezone

from fraudalert.anomaly import get_detector
from fraudalert.anomaly.features import FEATURE_NAMES, TxnView, compute_features, to_vector

T0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def history(n=30):
    return [TxnView(T0 + timedelta(days=i), 20.0 + (i % 5), "COFFEE SHOP", False) for i in range(n)]


def test_feature_vector_is_stable():
    f = compute_features(TxnView(T0 + timedelta(days=40), 22.0, "Coffee  Shop", False), history())
    assert list(f) == FEATURE_NAMES
    assert len(to_vector(f)) == len(FEATURE_NAMES)
    assert f["merchant_seen_log"] > 0  # merchant matching is case/space-insensitive


def test_baseline_needs_history_then_ranks_outliers():
    det = get_detector("baseline")
    assert det.score(compute_features(TxnView(T0, 20.0, "X", False), history(3))) is None
    normal = det.score(compute_features(TxnView(T0 + timedelta(days=40), 22.0, "COFFEE SHOP", False), history()))
    weird = det.score(compute_features(
        TxnView((T0 + timedelta(days=40)).replace(hour=3), 2500.0, "ELECTRONICS HK", True), history()))
    assert 0 <= normal < 0.3
    assert weird > 0.9
