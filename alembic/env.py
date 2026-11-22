from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from sqlalchemy import Connection, Engine, engine_from_config, event, pool

from alembic import context
from runway.db import models  # noqa: F401
from runway.db.base import Base

config = context.config
if config.config_file_name is not None:
    # Migrations run inside app startup; disabling the application's loggers
    # here would silence all logging emitted after the upgrade completes.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def make_sqlite_ddl_transactional(engine: Engine) -> None:
    """Let a failed SQLite revision roll back completely, DDL included.

    pysqlite by default issues no BEGIN before DDL and commits before it, so a
    crash midway through a revision (for example after batch mode created
    `_alembic_tmp_<table>`) left the database half-migrated. This is
    SQLAlchemy's documented pysqlite recipe: pysqlite stops managing
    transactions and SQLAlchemy emits its own BEGIN, so CREATE, DROP, ALTER
    and the alembic_version update share one transaction per revision.
    """

    @event.listens_for(engine, "connect")
    def _no_pysqlite_transactions(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    sqlite = connectable.dialect.name == "sqlite"
    if sqlite:
        make_sqlite_ddl_transactional(connectable)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite DDL is transactional with the recipe above; one
            # transaction per revision keeps each revision all-or-nothing.
            transactional_ddl=True if sqlite else None,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
