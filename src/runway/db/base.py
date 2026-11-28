from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from runway.config import Settings


class Base(DeclarativeBase):
    pass


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


class Database:
    def __init__(self, settings: Settings):
        settings.ensure_directories()
        self.settings = settings
        self.engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
        _configure_sqlite(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def immediate_session(self) -> Generator[Session, None, None]:
        """SQLite session serialized across processes from its first statement.

        SQLAlchemy's SQLite sessions normally use deferred transactions, so a
        check-then-write critical section can read the same state in two
        processes before either one becomes a writer. BEGIN IMMEDIATE acquires
        SQLite's database write lock up front; concurrent writers wait for the
        first transaction to commit or roll back. This must be the first
        statement in the session.
        """
        with self.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session
