from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
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


def init_db() -> None:
    from fraudalert import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())


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
