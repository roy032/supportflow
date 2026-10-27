"""Alembic environment. Runs against a connection handed over by app.db.migrate, or
against DATABASE_URL when invoked from the `alembic` command line."""

from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (register models on Base.metadata)
from app.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    from app.config import get_settings

    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't ALTER most things in place; batch mode rebuilds the table instead.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported; run against a database")
run_migrations_online()
