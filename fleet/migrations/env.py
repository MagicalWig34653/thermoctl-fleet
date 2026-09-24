"""Alembic environment for `fleet/migrations` (P1.3).

Configured **programmatically** by `fleet/storage.py::upgrade` via
`alembic.config.Config()` -- never from a repository-root `alembic.ini`,
which would not exist in the fleet Docker image (`docker/Dockerfile.fleet`
only `COPY`s `fleet/`). `config.config_file_name` is therefore always `None`
when this module runs, so the `fileConfig` step below is a no-op guard, kept
for parity with a conventional Alembic setup (and with thermoctl's own
`migrations/env.py`, same guard, same reason) rather than because it ever
fires here.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from fleet.storage import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)  # pragma: no cover -- never set, see above

target_metadata = Base.metadata


def run_migrations_offline() -> None:  # pragma: no cover -- not exercised by tests
    """`alembic upgrade --sql` style, without a live connection. Unused by
    `fleet.storage.upgrade` (which always runs online), kept for anyone
    invoking `alembic` directly on the command line."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite has no `ALTER TABLE` for constraints -- Alembic instead
            # rebuilds the table. Harmless for other dialects, required here.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():  # pragma: no cover -- not exercised by tests
    run_migrations_offline()
else:
    run_migrations_online()
