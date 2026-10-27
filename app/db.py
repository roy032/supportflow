"""Database engine and session setup (SQLAlchemy 2.0)."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # pre_ping replaces connections the server closed while idle (restarts, failovers).
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# The schema that `Base.metadata.create_all` produced before migrations existed.
BASELINE_REVISION = "0001"

# Any constant works; it only has to be the same in every process that migrates.
MIGRATION_LOCK_ID = 7_336_501


def _alembic_config(connection):
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def init_db(engine: Engine) -> None:
    """Bring the database schema up to date by applying any pending migrations.

    Databases created before migrations existed have the tables but no version
    record; they are stamped at the baseline first, then upgraded like any other.
    """
    from alembic import command

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # The web server and workers all migrate on startup; only one may do it at a
            # time. The lock is released when this transaction ends.
            connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        config = _alembic_config(connection)
        tables = set(inspect(connection).get_table_names())
        if "events" in tables and "alembic_version" not in tables:
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
