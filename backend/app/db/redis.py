import json
from redis.asyncio import Redis
from arq.connections import RedisSettings as ArqRedisSettings, ArqRedis
from arq import create_pool as arq_create_pool
import os


redis_client: Redis | None = None
_arq_pool: ArqRedis | None = None
# Phase 312.1 (D-15/D-16): the operator-visible name is VALKEY_URL and the default host is the
# renamed service. The `redis://` scheme and this module's name are protocol-level and stay.
VALKEY_URL = os.getenv("VALKEY_URL", "redis://valkey:6379/0")


async def init_redis() -> Redis:
    global redis_client
    redis_client = await Redis.from_url(
        VALKEY_URL,
        max_connections=10,
        decode_responses=True,
    )
    return redis_client


async def close_redis():
    global redis_client
    # Detach the module-global first: pytest-asyncio gives every test its own
    # event loop, so a client created in an earlier test's loop can outlive it.
    # aclose() on such a client raises RuntimeError("Event loop is closed") —
    # clearing the reference unconditionally keeps that stale, loop-bound
    # client from leaking into subsequent tests (full-suite cascade).
    client, redis_client = redis_client, None
    if client is None:
        return
    try:
        await client.aclose()
    except RuntimeError:
        pass


async def get_redis() -> Redis:
    if redis_client is None:
        raise RuntimeError("Redis not initialized")
    return redis_client


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        rest = VALKEY_URL[8:] if VALKEY_URL.startswith("redis://") else VALKEY_URL
        user_info = None
        host_part = rest
        if "@" in rest:
            user_info, host_part = rest.split("@", 1)
        host = "localhost"
        port = 6379
        db = 0
        password = None
        username = None
        if ":" in host_part and host_part.count(":") == 1:
            host_port, *db_part = host_part.split("/", 1)
            host, port_str = host_port.split(":", 1)
            port = int(port_str)
            if db_part:
                try:
                    db = int(db_part[0])
                except ValueError:
                    pass
        elif "/" in host_part:
            host, db_str = host_part.split("/", 1)
            if db_str:
                try:
                    db = int(db_str)
                except ValueError:
                    pass
        else:
            host = host_part if host_part else "localhost"
        if user_info:
            if ":" in user_info:
                username, password = user_info.split(":", 1)
            else:
                username = user_info
        settings = ArqRedisSettings(
            host=host, port=port, database=db,
            username=username, password=password if password else None,
        )
        _arq_pool = await arq_create_pool(settings, job_serializer=json.dumps, job_deserializer=json.loads)
    return _arq_pool


async def close_arq_pool():
    global _arq_pool
    if _arq_pool:
        await _arq_pool.aclose()
        _arq_pool = None
