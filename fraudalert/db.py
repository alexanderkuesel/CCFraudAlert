import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from fraudalert.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def configure(database_url: str | None = None) -> None:
    """(Re)configure the engine. Called lazily, or explicitly by tests."""
    global _engine, _SessionLocal
    url = database_url or get_settings().database_url
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    _engine = create_engine(url, pool_pre_ping=True, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def get_engine():
    if _engine is None:
        configure()
    return _engine


# Arbitrary constants identifying Postgres advisory locks.
_INIT_LOCK_KEY = 0x46524155  # "FRAU"
SYNC_LOCK_KEY = 0x53594E43  # "SYNC"


def init_db() -> None:
    """Create tables and seed default rules. Safe to call from several processes at once.

    The web and worker containers both start at the same time; without the lock their
    CREATE TABLEs race and one of them crashes on a duplicate-table error.
    """
    from fraudalert import models  # noqa: F401  (register tables)
    from fraudalert.pipeline import seed_default_rules

    with get_engine().begin() as conn:
        if conn.dialect.name == "postgresql":
            # Held until this transaction commits, i.e. until tables and seed rows exist.
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_LOCK_KEY})
        Base.metadata.create_all(conn)
        added_columns = _add_missing_columns(conn)
        with Session(bind=conn) as session:
            added = seed_default_rules(session)
            session.flush()
    if not _has_transactions():
        return
    from fraudalert.pipeline import reevaluate_all

    if REPARSE_WHEN_ADDED & added_columns:
        # New parsed fields (e.g. the bank's authorization code): re-read the stored emails once to fill
        # them in. This updates transactions in place, so labels and comments are kept.
        log.info("new columns %s: re-parsing stored emails to fill them", sorted(REPARSE_WHEN_ADDED & added_columns))
        reevaluate_all(reparse="all")
    elif RESCORE_WHEN_ADDED & added_columns:
        log.info("new columns %s: re-scoring stored transactions", sorted(RESCORE_WHEN_ADDED & added_columns))
        reevaluate_all()
    elif added:
        # New built-in rules (after an upgrade): apply them to the transactions already stored.
        log.info("added %d built-in rule(s); re-evaluating stored transactions", added)
        reevaluate_all()


def _has_transactions() -> bool:
    from fraudalert.models import Transaction

    with session_scope() as s:
        return s.query(Transaction.id).first() is not None


# Columns filled by the email parser: when an upgrade adds one, stored emails are re-parsed once.
REPARSE_WHEN_ADDED = {"transactions.auth_code", "transactions.reference"}
# Columns filled by scoring: when an upgrade adds one, stored transactions are re-scored once.
RESCORE_WHEN_ADDED = {"transactions.anomaly_reasons"}


def _add_missing_columns(conn) -> set[str]:
    """Minimal schema upgrade: create_all() makes missing tables but never alters existing ones,
    so add any model column an older database lacks. Only nullable, default-less columns can be
    added this way; anything more involved needs a real migration."""
    inspector = inspect(conn)
    added: set[str] = set()
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable or column.server_default is not None:
                raise RuntimeError(f"cannot auto-add {table.name}.{column.name}; it needs a migration")
            ddl_type = column.type.compile(dialect=conn.dialect)
            prep = conn.dialect.identifier_preparer
            conn.execute(text(f"ALTER TABLE {prep.quote(table.name)} ADD COLUMN {prep.quote(column.name)} {ddl_type}"))
            log.info("added column %s.%s", table.name, column.name)
            added.add(f"{table.name}.{column.name}")
    return added


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        configure()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def try_advisory_lock(key: int) -> Iterator[bool]:
    """Non-blocking lock shared by every process using the database (web, worker, CLI).

    Yields True if acquired. On SQLite (tests, single process) it always succeeds.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as conn:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar())
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                conn.commit()
