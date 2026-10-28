"""Best-effort, fail-open purge of the public type-schema cache tier (plan 207-01/02).

The type-schema tier (``cache:public:objecttype-schema:{object_type_id}``,
written by ``object_read_service._get_type_schema``) has a 600s TTL as its
staleness safety net (D-03); correctness after schema mutations depends on
explicit purge (D-04/D-05). This module is the purge side of the tier.

It also owns the purge of the per-client definition payloads
(``purge_public_definition_caches``), which live in the same public cache
namespace but are keyed per client.

Imports are intentionally limited to ``app.db.redis`` — importing any
service module (or ``object_read_service``) here would create a circular
import (public_api modules import services, not vice versa).
"""

from app.db.redis import get_redis


async def purge_public_objecttype_schema_cache(object_type_ids: list[str]) -> None:
    """Best-effort, fail-open purge of the type-schema tier (D-03/D-04).

    Deletes the exact key ``cache:public:objecttype-schema:{oid}`` for
    each object type id — plan 01's tier is ONE flat value per type (no
    user/attrs sub-keying), so this is a direct delete, NOT a SCAN-by-prefix
    (a ``:*`` glob would never match a key with no trailing separator and
    would silently no-op every purge). On any Redis error the purge
    silently fails open — the tier's long TTL is the staleness safety net
    (D-03). This intentionally does NOT reuse the SCAN idiom from
    ``purge_public_object_cache`` (object_write_service.py:45) verbatim —
    that idiom exists because ITS keys fan out by user/attrs suffix; this
    tier has no such fan-out.
    """
    try:
        redis = await get_redis()
        for oid in object_type_ids:
            await redis.delete(f"cache:public:objecttype-schema:{oid}")
    except Exception:
        pass


async def purge_public_definition_caches(
    object_type_ids: list[str], schema_ids: list[str]
) -> None:
    """Best-effort, fail-open purge of the per-client definition payloads.

    ``public_api/attributes.py`` renders the attribute definitions per client
    into ``cache:public:objecttype:{object_type_id}:attrs:{user_id}`` and
    ``cache:public:schema:{schema_id}:attrs:{user_id}`` (60s TTL, written by
    ``set_cached`` without tags). Those payloads carry the attribute
    classification markers, and their shape guard only requires the current
    list/key shape, which a pre-change entry still satisfies — so an attribute
    classification change must purge them explicitly, otherwise every client
    keeps reading the old labels until the TTL expires. Neither the tag
    invalidation nor :func:`purge_public_objecttype_schema_cache` reaches these
    keys.

    The object-type keys are type-scoped, so the caller passes the affected
    type ids (the type plus its descendants, whose payloads inherit the
    attribute). The schema keys are NOT type-scoped — one payload lists every
    type of its schema — so the caller passes the affected schema ids instead
    of this module scanning the whole ``cache:public:schema`` namespace.

    Fail-open like the tier purge above: a Redis error never fails the write,
    the short TTL bounds the staleness.
    """
    try:
        redis = await get_redis()
        patterns = [
            *(f"cache:public:objecttype:{oid}:attrs:*" for oid in object_type_ids),
            *(f"cache:public:schema:{sid}:attrs:*" for sid in schema_ids),
        ]
        for pattern in patterns:
            # Batched SCAN (the Redis default COUNT of 10 would make a sweep
            # over the shared database thousands of round trips).
            batch: list[str] = []
            async for key in redis.scan_iter(pattern, count=1000):
                batch.append(key)
                if len(batch) >= 1000:
                    await redis.delete(*batch)
                    batch.clear()
            if batch:
                await redis.delete(*batch)
    except Exception:
        pass
