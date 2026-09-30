from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from fraudalert.config import get_settings


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


# Arbitrary constant identifying the schema-setup advisory lock.
_INIT_LOCK_KEY = 0x46524155  # "FRAU"


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
        with Session(bind=conn) as session:
            seed_default_rules(session)
            session.flush()


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
