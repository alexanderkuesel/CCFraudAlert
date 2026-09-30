from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from fraudalert import pipeline
from fraudalert.models import RawEmail, Rule, Transaction

from .conftest import make_eml

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def write(tmp_path, name, subject, body, when):
    p = tmp_path / name
    p.write_bytes(make_eml(subject, body, when))
    return p


def test_end_to_end_import_rules_and_reevaluate(db, tmp_path):
    files = [
        write(tmp_path, f"small{i}.eml", "Purchase alert", f"You made a ${10 + i}.00 purchase at CORNER DELI.",
              NOW - timedelta(days=20 - i))
        for i in range(12)
    ]
    files += [
        write(tmp_path, "big.eml", "Purchase alert", "You made a $450.00 purchase at BEST BUY.", NOW - timedelta(days=3)),
        write(tmp_path, "eur.eml", "Purchase alert", "A charge of EUR 12,00 at BOULANGERIE PAUL.", NOW - timedelta(days=2)),
        write(tmp_path, "junk.eml", "Your statement is ready", "Balance: $900.00", NOW - timedelta(days=1)),
    ]
    result = pipeline.import_eml_files(files)
    assert (result.fetched, result.parsed, result.failed, result.flagged) == (15, 14, 1, 2)

    # Re-importing is a no-op (dedup on Message-ID).
    again = pipeline.import_eml_files(files)
    assert again.fetched == 0

    with db.session_scope() as s:
        flagged = {t.merchant for t in s.scalars(select(Transaction).where(Transaction.flagged.is_(True)))}
        assert flagged == {"BEST BUY", "BOULANGERIE PAUL"}
        eur = s.scalar(select(Transaction).where(Transaction.merchant == "BOULANGERIE PAUL"))
        assert eur.is_foreign and eur.currency == "EUR"
        assert eur.anomaly_score is not None and eur.features["is_foreign"] == 1.0
        first = s.scalars(select(Transaction).order_by(Transaction.occurred_at)).first()
        assert first.anomaly_score is None  # not enough history yet
        assert s.scalar(select(RawEmail).where(RawEmail.parse_status == "failed")).subject == "Your statement is ready"

        # Add a rule on the fly and apply it to history.
        s.add(Rule(name="Deli", match="all", conditions=[{"field": "merchant", "op": "contains", "value": "deli"}]))
    r = pipeline.reevaluate_all()
    assert r.flagged == 14

    with db.session_scope() as s:
        for rule in s.scalars(select(Rule)):
            rule.enabled = False
    assert pipeline.reevaluate_all().flagged == 0
    with db.session_scope() as s:
        assert not s.scalars(select(Transaction).where(Transaction.flagged.is_(True))).all()


def test_webhook_only_for_recent(db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "notify", lambda settings, txn, reasons: calls.append(txn.merchant) or True)
    pipeline.import_eml_files([
        write(tmp_path, "old.eml", "Alert", "You spent $999.00 at OLD STORE.", NOW - timedelta(days=30)),
        write(tmp_path, "new.eml", "Alert", "You spent $999.00 at NEW STORE.", NOW - timedelta(hours=1)),
    ])
    assert calls == ["NEW STORE"]


def test_concurrent_ingest_of_same_email_is_not_an_error(db, tmp_path, monkeypatch):
    """Worker and a manual `fraudalert sync` can fetch the same email at the same moment: both pass
    the "already stored?" check, then race to insert it. The loser must skip it, not crash."""
    import threading

    from fraudalert.anomaly import get_detector
    from fraudalert.config import get_settings
    from fraudalert.ingest.message import parse_rfc822

    msg = parse_rfc822(make_eml("Alert", "You spent $20.00 at RACE CAFE.", NOW - timedelta(hours=1)))
    barrier = threading.Barrier(2, timeout=10)

    class RacingRawEmail(pipeline.RawEmail):
        def __init__(self, **kw):
            barrier.wait()  # both threads have passed the existence check before either inserts
            super().__init__(**kw)

    monkeypatch.setattr(pipeline, "RawEmail", RacingRawEmail)
    results, errors = [], []

    def ingest():
        r = pipeline.SyncResult()
        try:
            with db.session_scope() as s:
                pipeline.ingest_message(s, msg, get_settings(), get_detector(), pipeline.load_rules(s), r)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        results.append(r)

    threads = [threading.Thread(target=ingest) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.undo()
    assert errors == []
    assert sorted(r.fetched for r in results) == [0, 1]
    with db.session_scope() as s:
        assert len(s.scalars(select(Transaction)).all()) == 1
