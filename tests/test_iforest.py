import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from fraudalert import pipeline
from fraudalert.anomaly import get_detector
from fraudalert.anomaly.features import TxnView, compute_features
from fraudalert.anomaly.iforest import MIN_SAMPLES, fit_forest
from fraudalert.anomaly.training import NotEnoughData, needs_retrain, train_iforest
from fraudalert.models import AnomalyModel, Transaction

T0 = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
SHOPS = [("SUPERMARKET", 30, 90), ("COFFEE", 3, 7), ("GAS", 35, 55), ("PHARMACY", 8, 25)]


def history(n=150, seed=1):
    rnd = random.Random(seed)
    views = []
    for i in range(n):
        shop, lo, hi = rnd.choice(SHOPS)
        when = T0 + timedelta(hours=18 * i + rnd.randint(0, 5))  # daytime-ish, steady pace
        views.append(TxnView(when.replace(hour=rnd.randint(9, 19)), rnd.uniform(lo, hi), shop, False))
    return views


def featurize(views):
    return [compute_features(v, views[:i]) for i, v in enumerate(views)]


def test_forest_ranks_outliers_high_and_normal_spend_low():
    views = history()
    model = fit_forest(featurize(views))
    nxt = views[-1].occurred_at + timedelta(hours=18)
    normal = compute_features(TxnView(nxt.replace(hour=12), 45.0, "SUPERMARKET", False), views)
    odd = compute_features(TxnView(nxt.replace(hour=3), 2400.0, "LUXE ELECTRONICS DUBAI", True), views)
    n_score, o_score = model.score_many([normal, odd])
    assert o_score >= 0.97 and n_score < 0.8
    assert 0 <= n_score <= 1 and 0 <= o_score <= 1


def add_history(db, n):
    """Store n ordinary transactions with features, like the pipeline would."""
    with db.session_scope() as s:
        views = history(n)
        for v, f in zip(views, featurize(views)):
            s.add(Transaction(merchant=v.merchant, amount=round(v.amount, 2), currency="USD", card_last4="1111",
                              occurred_at=v.occurred_at, features=f))


def test_falls_back_to_baseline_until_trained(db):
    det = get_detector("iforest")
    assert det.name == "baseline-v1"  # no model yet: the score is honestly labelled
    add_history(db, MIN_SAMPLES - 1)
    with db.session_scope() as s, pytest.raises(NotEnoughData, match=f"need at least {MIN_SAMPLES}"):
        train_iforest(s)
    with db.session_scope() as s:
        assert not needs_retrain(s)


def test_train_store_reload_and_rescore(db):
    add_history(db, 120)
    with db.session_scope() as s:
        s.add(Transaction(merchant="LUXE ELECTRONICS", amount=2400, currency="USD", card_last4="1111",
                          occurred_at=T0 + timedelta(days=100), label_fraud=True,
                          features=compute_features(TxnView(T0 + timedelta(days=100, hours=-9), 2400, "LUXE ELECTRONICS", True),
                                                    history(120))))
        s.scalars(select(Transaction)).first().label_fraud = False
        assert needs_retrain(s)
    info = pipeline.retrain_anomaly_model()
    assert info["n_samples"] == 120  # the fraud-labelled one is excluded from training
    m = info["metrics"]
    assert (m["labelled_fraud"], m["labelled_legit"]) == (1, 1)
    assert m["mean_fraud_iforest"] > 0.9 and m["auc_iforest"] == 1.0
    det = get_detector("iforest")
    assert det.name.startswith("iforest-v1@")
    with db.session_scope() as s:
        # everything was re-scored by the new model
        models = {t.anomaly_model for t in s.scalars(select(Transaction))}
        assert all(name.startswith("iforest-v1@") for name in models)
        fraud = s.scalar(select(Transaction).where(Transaction.label_fraud.is_(True)))
        assert fraud.anomaly_score >= 0.97
        # and the Low-priority built-in alarm fired on it
        assert any(a.rule and a.rule.name == "Unusual pattern (anomaly model)" and a.severity == "low" for a in fraud.alerts)
        assert not needs_retrain(s)  # fresh model, nothing new


def test_keeps_only_the_newest_models(db):
    add_history(db, 60)
    for _ in range(7):
        with db.session_scope() as s:
            train_iforest(s)
    with db.session_scope() as s:
        assert len(s.scalars(select(AnomalyModel.id)).all()) == 5


def test_settings_page_shows_model_and_retrains(db):
    from fastapi.testclient import TestClient

    from fraudalert.web.app import create_app

    client = TestClient(create_app(init=False))
    page = client.get("/settings").text
    assert "Anomaly model" in page and "not trained yet" in page
    r = client.post("/model/train")
    assert "need at least 50 transactions" in r.text
    add_history(db, 80)
    r = client.post("/model/train")
    assert "Trained the anomaly model on 80 transactions" in r.text
    assert "Isolation Forest" in client.get("/settings").text


def test_cli_train(db, capsys):
    from fraudalert.cli import main

    assert main(["train"]) == 1
    assert "need at least" in capsys.readouterr().err
    add_history(db, 70)
    assert main(["train"]) == 0
    assert "trained on 70 transactions" in capsys.readouterr().out
