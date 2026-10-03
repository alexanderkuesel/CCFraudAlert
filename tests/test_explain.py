import math
from datetime import timedelta

from sqlalchemy import select

from fraudalert import pipeline
from fraudalert.anomaly.explain import NEUTRAL, describe, explain, is_atypical, summary
from fraudalert.anomaly.features import TxnView, compute_features
from fraudalert.anomaly.iforest import fit_forest
from fraudalert.models import Rule, Transaction

from .test_iforest import add_history, featurize, history


def hour_feats(h):
    return {"hour_sin": math.sin(2 * math.pi * h / 24), "hour_cos": math.cos(2 * math.pi * h / 24)}


def test_wording():
    assert describe("amount", {"log_amount": 0.0}) == "zero or near-zero amount (a typical card test)"
    assert describe("merchant", {"merchant_seen_log": 0.0}) == "first purchase at this merchant"
    assert describe("merchant", {"merchant_seen_log": math.log1p(1)}) == "only 1 earlier purchase here"
    assert describe("time", hour_feats(3), hour_feats(19)) == "at 3 am (you usually shop around 7 pm)"
    assert describe("burst", {"txns_last_24h": 5}) == "5 other transactions in the past 24 hours"
    assert describe("gap", {"hours_since_prev_log": math.log1p(0.1)}) == "minutes after your previous transaction"
    assert summary([{"text": "a"}, {"text": "b"}]) == "a · b" and summary(None) == ""


def test_only_genuinely_unusual_values_become_reasons():
    typical = {**NEUTRAL, **hour_feats(19)}
    assert not is_atypical("time", hour_feats(17), typical)  # 2h from usual: not a reason
    assert is_atypical("time", hour_feats(3), typical)
    assert not is_atypical("amount_merchant", {"amount_z_merchant": 0.8}, typical)
    assert not is_atypical("foreign", {"is_foreign": 0.0}, typical)

    # a toy model where every feature contributes, but only some values are actually unusual
    def raw(rows):
        return [sum(abs(r.get(k, 0) - NEUTRAL.get(k, 0)) for k in NEUTRAL) for r in rows]

    f = {**NEUTRAL, "log_amount": 0.0, "amount_z_global": -3.0, "merchant_seen_log": 0.0, **hour_feats(13)}
    texts = [r["text"] for r in explain(raw, f, NEUTRAL)]
    assert "zero or near-zero amount (a typical card test)" in texts and "first purchase at this merchant" in texts
    assert not any(t.startswith("at ") for t in texts)  # 1 pm vs a noon "typical": not a reason
    assert explain(raw, dict(NEUTRAL), NEUTRAL) == []  # nothing unusual -> no reasons


def test_isolation_forest_explains_an_outlier():
    views = history()
    model = fit_forest(featurize(views))
    nxt = views[-1].occurred_at + timedelta(hours=18)
    odd = compute_features(TxnView(nxt.replace(hour=3), 2400.0, "LUXE ELECTRONICS DUBAI", True), views)
    from fraudalert.anomaly.explain import explain as run

    texts = [r["text"] for r in run(model.raw, odd, model.medians)]
    assert len(texts) <= 3 and "first purchase at this merchant" in texts
    assert any(t.startswith("at 3 am") for t in texts) or "large amount for you" in texts


def test_reasons_stored_for_notable_scores_and_shown(db, tmp_path):
    from fastapi.testclient import TestClient

    from fraudalert.web.app import create_app

    add_history(db, 90)
    pipeline.retrain_anomaly_model()
    with db.session_scope() as s:
        rows = s.scalars(select(Transaction)).all()
        notable = [t for t in rows if t.anomaly_score is not None and t.anomaly_score >= pipeline.EXPLAIN_MIN]
        assert notable and all(t.anomaly_reasons is None for t in rows if t.anomaly_score < pipeline.EXPLAIN_MIN)
        assert any(t.anomaly_reasons for t in notable)
        with_reason = next(t for t in notable if t.anomaly_reasons)
        merchant, reason = with_reason.merchant, with_reason.anomaly_reasons[0]["text"]
        with_reason.flagged = True  # make it show in the alarm views
    client = TestClient(create_app(init=False))
    html = client.get("/alarms?view=alarms").text  # it's flagged above; the journal pages 50 rows at a time
    assert "Why unusual:" in html and reason in html
    api = {t["merchant"]: t for t in client.get("/api/transactions?limit=1000").json() if t["anomaly_reasons"]}
    assert merchant in api

    net = client.get("/api/network?days=all").json()
    assert net["anomaly"]["isolation_forest"] and net["anomaly"]["limit"] == 0.97
    node = next(n for n in net["nodes"] if n.get("label") == merchant)
    assert node["scores"] == sorted(node["scores"], reverse=True) and node["reasons"]
    with db.session_scope() as s:  # the slider default follows the alarm rule's threshold
        rule = s.scalar(select(Rule).where(Rule.name == "Unusual pattern (anomaly model)"))
        rule.conditions = [{"field": "anomaly_score", "op": "gte", "value": 0.9}]
    assert client.get("/api/network?days=all").json()["anomaly"]["limit"] == 0.9
    page = client.get("/network").text
    assert 'id="limit"' in page and "Beyond the anomaly limit" in page


def test_upgrade_rescores_to_fill_reasons(db):
    from sqlalchemy import text

    add_history(db, 70)
    pipeline.retrain_anomaly_model()
    with db.get_engine().begin() as conn:
        conn.execute(text("ALTER TABLE transactions DROP COLUMN anomaly_reasons"))
    db.init_db()
    with db.session_scope() as s:
        assert any(t.anomaly_reasons for t in s.scalars(select(Transaction)))
