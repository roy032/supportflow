"""Migration tests: the migrations build exactly the schema the models describe, and
databases created before migrations existed are upgraded in place."""

import pytest

pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, _alembic_config, init_db  # noqa: E402


def memory_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def test_migrations_match_the_models():
    engine = memory_engine()
    init_db(engine)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_pre_migration_database_is_stamped_and_upgraded():
    engine = memory_engine()
    # The schema as `create_all` made it before the job-queue columns existed.
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, external_id VARCHAR(128) NOT NULL,"
            " source VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL, payload JSON NOT NULL,"
            " customer_name VARCHAR(200), customer_email VARCHAR(320), message TEXT NOT NULL,"
            " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO events (external_id, source, status, payload, message, created_at,"
            " updated_at) VALUES ('old', 'tally', 'failed', '{}', 'hi', '2026-01-01',"
            " '2026-01-01')"
        ))
        connection.execute(text(
            "CREATE TABLE classifications (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL)"
        ))
        connection.execute(text("CREATE TABLE failures (id INTEGER PRIMARY KEY)"))

    init_db(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("events")}
    assert {"attempts", "next_attempt_at", "locked_until"} <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, attempts, next_attempt_at FROM events")
        ).one()
    # Existing rows keep their data and are not scheduled automatically.
    assert tuple(row) == ("failed", 0, None)


def test_init_db_is_idempotent():
    engine = memory_engine()
    init_db(engine)
    init_db(engine)


def test_every_migration_downgrades_cleanly():
    engine = memory_engine()
    init_db(engine)
    with engine.begin() as connection:
        command.downgrade(_alembic_config(connection), "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    init_db(engine)
    assert {"events", "classifications", "failures"} <= set(inspect(engine).get_table_names())
