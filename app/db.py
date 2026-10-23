"""Database engine and session setup (SQLAlchemy 2.0)."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# The schema that `Base.metadata.create_all` produced before migrations existed.
BASELINE_REVISION = "0001"


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
