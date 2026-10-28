from hashlib import md5
import json
import logging
import random

from app.db.redis import get_redis

logger = logging.getLogger(__name__)

CACHE_TTL_LIST = 60
CACHE_TTL_ENTITY = 300

# Per-entity TTL tiers:
#   Static       – 900s entity, 300s list
#   Semi-static  – 300s entity, 120s list
#   Volatile     –  60s entity,  30s list
ENTITY_TTLS: dict[str, dict[str, int]] = {
    # Static
    "source":          {"entity": 900, "list": 300},
    "icon":            {"entity": 900, "list": 300},
    "attribute_type":  {"entity": 900, "list": 300},
    "reference_type":  {"entity": 900, "list": 300},
    # Semi-static
    "schema":          {"entity": 300, "list": 120},
    "object_type":     {"entity": 300, "list": 120},
    "status":          {"entity": 300, "list": 120},
    "target":          {"entity": 300, "list": 120},
    # Volatile
    "object":          {"entity": 60,  "list": 30},
    "object_value":    {"entity": 60,  "list": 30},
    "object_ref":      {"entity": 60,  "list": 30},
    "task":            {"entity": 60,  "list": 30},
    "import":          {"entity": 60,  "list": 30},
    "export":          {"entity": 60,  "list": 30},
    "schedule":        {"entity": 60,  "list": 30},
    "service_account": {"entity": 60,  "list": 30},
    "user_permissions": {"entity": 60, "list": 60},
}


def _resolve_ttl(entity_type: str | None, ttl: int | None, is_list: bool = False) -> int:
    """Resolve effective TTL from optional entity_type + optional explicit ttl.

    Explicit ttl always wins. Falls back to per-entity TTL if entity_type is
    known, otherwise returns the module-level default constants.
    """
    if ttl is not None:
        return ttl
    if entity_type and entity_type in ENTITY_TTLS:
        return ENTITY_TTLS[entity_type]["list" if is_list else "entity"]
    return CACHE_TTL_LIST if is_list else CACHE_TTL_ENTITY


def build_cache_key(entity_type: str, entity_id: str) -> str:
    return f"cache:{entity_type}:{entity_id}"


def build_list_cache_key(entity_type: str, **params) -> str:
    params_hash = md5(
        str(sorted(params.items())).encode()
    ).hexdigest()[:8]
    return f"cache:{entity_type}:list:{params_hash}"


def build_tag_key(entity_type: str) -> str:
    return f"tag:{entity_type}"


async def get_cached(key: str) -> dict | list | None:
    try:
        redis = await get_redis()
        value = await redis.get(key)
        if value is not None:
            result = json.loads(value)
            # ~1% sampled logging for observability
            if random.random() < 0.01:
                logger.info("cache:%s:hit", key)
            return result
        # ~1% sampled logging for observability
        if random.random() < 0.01:
            logger.info("cache:%s:miss", key)
        return None
    except Exception as e:
        logger.exception("Cache get error for key %s", key)
        return None


async def set_cached(key: str, value: dict | list, ttl: int | None = None, entity_type: str | None = None):
    try:
        redis = await get_redis()
        resolved_ttl = _resolve_ttl(entity_type, ttl, is_list=False)
        await redis.setex(key, resolved_ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.exception("Cache set error for key %s", key)


async def set_cached_with_tags(
    key: str, value: dict | list, tags: list[str],
    ttl: int | None = None, entity_type: str | None = None,
    is_list: bool = True,
):
    try:
        resolved_ttl = _resolve_ttl(entity_type, ttl, is_list=is_list)
        await set_cached(key, value, resolved_ttl, entity_type)
        redis = await get_redis()
        for tag in tags:
            await redis.sadd(tag, key)
            await redis.expire(tag, resolved_ttl)
    except Exception as e:
        logger.exception("Cache set with tags error for key %s", key)


async def invalidate_by_tags(tags: list[str]):
    try:
        redis = await get_redis()
        for tag in tags:
            keys = await redis.smembers(tag)
            if keys:
                await redis.delete(*keys, tag)
            else:
                await redis.delete(tag)
    except Exception as e:
        logger.exception("Cache invalidate error for tags %s", tags)


async def invalidate_entity_cache(entity_type: str):
    await invalidate_by_tags([build_tag_key(entity_type)])


async def invalidate_user_permissions_cache(user_ids: list) -> None:
    try:
        redis = await get_redis()
        keys = [build_cache_key("user_permissions", str(uid)) for uid in user_ids]
        if keys:
            await redis.delete(*keys)
    except Exception:
        logger.exception("Failed to invalidate user_permissions cache")
