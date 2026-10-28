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
def get_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url
    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_user = os.getenv("DB_USER", "postgres")
    # No credential default: an unset DB_PASSWORD must leave the URL empty-password (the
    # server rejects it) instead of silently becoming a working credential. Compose and the
    # Helm chart both inject it from their secret source, and the DB_PASSWORD guard in
    # docker-compose.yml aborts interpolation long before this line runs.
    db_password = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "app")
    return f"postgresql+asyncpg://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


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