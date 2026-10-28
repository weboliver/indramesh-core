import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.db.base import Base
from app.db import get_database_url
from app.db.migration_filters import include_object
from app.logging import configure_logging
from app.models import (
    Source, Schema, Icon, ObjectType, Object, ObjectTypeAttribute, ObjectValue,
    ObjectRef, AttributeType, ReferenceType, Status, Import, Export,
    ImportStatus, ExportStatus, Task, Target, ServiceAccount,
    AuditLog, AuditLogConfig,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# D-18: alembic must not disable existing loggers and must not load the ini
# logging section with the stdlib one-shot helper. The central configuration is
# idempotent, so this is a no-op when env.py runs inside an already-configured
# process (e.g. the plugin migration runner). The ini logging sections become
# unused.
configure_logging("alembic")

config = context.config

config.set_main_option("sqlalchemy.url", get_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()