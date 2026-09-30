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
        assert s.scalar(select(func.count(Rule.id))) == 1  # seeded exactly once
    dbmod.Base.metadata.drop_all(dbmod.get_engine())
