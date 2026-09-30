import os
import threading

import pytest
from sqlalchemy import func, select

PG_URL = os.environ.get("FRAUDALERT_TEST_DATABASE_URL", "")


@pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="needs FRAUDALERT_TEST_DATABASE_URL=postgresql://...")
def test_concurrent_init_db_on_empty_database():
    """web + worker start together; both must survive creating the schema."""
    from fraudalert import db as dbmod
    from fraudalert.models import Rule

    dbmod.configure(PG_URL)
    dbmod.Base.metadata.drop_all(dbmod.get_engine())
    errors, barrier = [], threading.Barrier(6)

    def run():
        barrier.wait()
        try:
            dbmod.init_db()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with dbmod.session_scope() as s:
        from fraudalert.pipeline import DEFAULT_RULES

        assert s.scalar(select(func.count(Rule.id))) == len(DEFAULT_RULES)  # each seeded exactly once
    dbmod.Base.metadata.drop_all(dbmod.get_engine())


@pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="needs FRAUDALERT_TEST_DATABASE_URL=postgresql://...")
def test_sync_lock_is_shared_across_processes():
    """The worker container and a manual `fraudalert sync` are separate processes; only one may sync."""
    from fraudalert import db as dbmod

    dbmod.configure(PG_URL)
    with dbmod.try_advisory_lock(123) as first:
        assert first
        with dbmod.try_advisory_lock(123) as second:  # a different pooled connection = another session
            assert not second
    with dbmod.try_advisory_lock(123) as again:
        assert again  # released on exit


def test_init_db_adds_columns_missing_from_an_older_database(db):
    """Databases created before `transactions.comment` existed get the column on startup."""
    from sqlalchemy import inspect, text

    from fraudalert.models import Transaction

    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE transactions DROP COLUMN comment"))
    assert "comment" not in {c["name"] for c in inspect(engine).get_columns("transactions")}
    db.init_db()
    assert "comment" in {c["name"] for c in inspect(engine).get_columns("transactions")}
    with db.session_scope() as s:
        assert s.query(Transaction).count() == 0  # still queryable through the ORM


def test_upgrade_adds_bank_code_columns_and_backfills_them(db, tmp_path):
    """An install from before auth_code/reference existed gets the columns and the codes, keeping labels."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from fraudalert import pipeline
    from fraudalert.models import Transaction

    from .bac import bac_eml

    p = tmp_path / "bac.eml"
    p.write_bytes(bac_eml(datetime.now(timezone.utc) - timedelta(days=1)))
    pipeline.import_eml_files([p])
    with db.session_scope() as s:
        t = s.query(Transaction).one()
        t.label_fraud, t.comment = True, "reported"
    with db.get_engine().begin() as conn:  # simulate the old schema
        conn.execute(text("ALTER TABLE transactions DROP COLUMN auth_code"))
        conn.execute(text("ALTER TABLE transactions DROP COLUMN reference"))
    db.init_db()
    with db.session_scope() as s:
        t = s.query(Transaction).one()
        assert (t.auth_code, t.label_fraud, t.comment) == ("657401", True, "reported")
