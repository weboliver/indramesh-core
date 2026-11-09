from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

# Phase 98 — DATABASE_URL construction from component env vars (CONFIG-01)
# Separates secrets (credentials) from deployment artifacts (docker-compose.yml).
# Component vars allow dynamic reconfiguration without rebuilding containers.
# Shared with backend/alembic/env.py so there is exactly one place that knows how
# to build this URL — Alembic's own os.getenv("DATABASE_URL") read must not regress
# to alembic.ini's placeholder once the literal DATABASE_URL env var is removed.
#
# Phase 322 (DEPL-02, D-04/D-08) — two identities:
#   * get_database_url()            — the RUNTIME identity (DB_APP_*, DML only).
#   * get_maintenance_database_url() — the DB_USER identity for DDL/DCL paths
#     (Alembic, bootstrap_role, the plugin migration parent, app.db.roles).
# An explicit DATABASE_URL still wins for both, exactly as before; it is the
# documented escape hatch for an externally managed database.
def _component_database_url(db_user: str, db_password: str) -> str:
    """Assemble the async URL from the shared DB_* component variables."""
    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "app")
    return f"postgresql+asyncpg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


def get_database_url() -> str:
    """The RUNTIME URL: DB_APP_* when configured, otherwise the DB_USER fallback.

    ``DB_APP_USER`` empty is the documented fallback for externally managed
    databases that already connect with a restricted user (D-04), not an error.
    """
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url
    app_user = os.getenv("DB_APP_USER", "")
    if app_user:
        return _component_database_url(app_user, os.getenv("DB_APP_PASSWORD", ""))
    # No credential default: an unset DB_PASSWORD must leave the URL empty-password (the
    # server rejects it) instead of silently becoming a working credential. Compose and the
    # Helm chart both inject it from their secret source, and the DB_PASSWORD guard in
    # docker-compose.yml aborts interpolation long before this line runs.
    return _component_database_url(
        os.getenv("DB_USER", "postgres"), os.getenv("DB_PASSWORD", "")
    )


def get_maintenance_database_url() -> str:
    """The DB_USER URL used ONLY by DDL/DCL paths (D-08).

    Alembic, ``bootstrap_role``, the privileged plugin-migration parent and
    ``app.db.roles`` need ownership/CREATEROLE rights. Nothing in the request or
    worker path may use this builder — the runtime identity stays DML-only.
    """
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url
    return _component_database_url(
        os.getenv("DB_USER", "postgres"), os.getenv("DB_PASSWORD", "")
    )


# D-08 / Pitfall 10: an explicit DATABASE_URL outranks DB_APP_* and therefore
# disables the runtime/maintenance split. The weakened state must be visible at
# startup, and the process is never aborted. Fixed sentence: no URL, user or
# password is ever interpolated into it (T-322-04).
DATABASE_URL_SPLIT_DISABLED_WARNING = (
    "DATABASE_URL is set, so the runtime/maintenance database identity split is "
    "disabled: the application connects with the identity carried by that URL. "
    "Remove DATABASE_URL to run the application as DB_APP_USER."
)


def runtime_identity_source() -> str:
    """Which variable currently selects the runtime identity (D-08).

    ``"database_url"`` — an explicit URL wins and disables the split.
    ``"app_user"``     — the least-privilege runtime role (``DB_APP_USER``).
    ``"db_user"``      — the documented fallback to the maintenance identity.
    """
    if os.getenv("DATABASE_URL"):
        return "database_url"
    if os.getenv("DB_APP_USER", ""):
        return "app_user"
    return "db_user"


def log_identity_split_warning(logger) -> None:
    """Emit the split-disabled warning when, and only when, ``DATABASE_URL`` wins.

    Deliberately a helper rather than a log statement inside the URL builders:
    those run at import time, before logging is configured (Open Question 5).
    """
    if runtime_identity_source() == "database_url":
        logger.warning(DATABASE_URL_SPLIT_DISABLED_WARNING)


DATABASE_URL = get_database_url()


def get_pool_config() -> tuple[int, int, int, bool]:
    """
    Return (pool_size, max_overflow, pool_recycle, pool_pre_ping).

    Development defaults: pool_size=10, max_overflow=20, pool_recycle=1800, pool_pre_ping=True
    Override via environment: DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_RECYCLE, DB_POOL_PRE_PING
    """
    pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "20"))
    pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    pool_pre_ping = os.getenv("DB_POOL_PRE_PING", "true").lower() == "true"
    return pool_size, max_overflow, pool_recycle, pool_pre_ping


pool_size, max_overflow, pool_recycle, pool_pre_ping = get_pool_config()

engine = create_async_engine(
    DATABASE_URL,
    pool_size=pool_size,
    max_overflow=max_overflow,
    pool_recycle=pool_recycle,
    pool_pre_ping=pool_pre_ping,
    echo=False,
    # D-19: bound parameters disappear from StatementError texts.
    hide_parameters=True,
)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session