from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def for_update(stmt, *, skip_locked: bool = False):
    """`SELECT ... FOR UPDATE` that also refreshes rows the Session already holds.

    A plain `.with_for_update()` takes the row lock but returns whatever copy of
    the object is already in the identity map — typically one loaded *before* the
    lock, by an ownership check. Read-modify-write on that stale copy loses every
    concurrent update but the last: the lock serialises the writers and each one
    still computes from the same pre-lock value. `populate_existing` forces the
    locked row to overwrite the cached instance, which is what makes the lock
    mean anything.

    Every FOR UPDATE in this codebase must go through here.
    """
    return (
        stmt.with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
