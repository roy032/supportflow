"""Migration tests: the migrations build exactly the schema the models describe, and
databases created before migrations existed are upgraded in place."""

import os
import threading

import pytest

pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from app.db import Base, _alembic_config, init_db  # noqa: E402
from tests.conftest import make_test_engine as memory_engine  # noqa: E402


def test_migrations_match_the_models():
    engine = memory_engine()
    init_db(engine)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_pre_migration_database_is_stamped_and_upgraded():
    engine = memory_engine()
    # The baseline schema without a version table: what `create_all` made before
    # migrations existed.
    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "0001")
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text(
            "INSERT INTO events (external_id, source, status, payload, message, created_at,"
            " updated_at) VALUES ('old', 'tally', 'failed', '{}', 'hi', '2026-01-01',"
            " '2026-01-01')"
        ))

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


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="concurrent startup migrations are only guarded on Postgres",
)
def test_concurrent_startups_migrate_once():
    from app.db import make_engine

    memory_engine().dispose()  # wipe the schema
    errors = []

    def start():
        try:
            init_db(make_engine(os.environ["TEST_DATABASE_URL"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
