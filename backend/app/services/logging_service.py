"""Atomic logging-level read/mutation service (D-01..D-08, D-22, D-23).

The only writer of the reserved ``logging.levels`` key. The generic
``set_setting`` seam refuses this key (``ReservedSettingKeyError``); GET/PUT
project fresh database truth and the process env basis, never controller
memory.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from fastapi import HTTPException
from sqlalchemy import select, text

from app.logging.categories import category_for, is_valid_category
from app.logging.levels import (
    CORE_CATEGORIES,
    DISCOVERY_KEY_PREFIX,
    LEVEL_VALUES,
    LEVEL_VERSION_KEY,
    LOGGING_LEVELS_KEY,
    MAX_BODY_BYTES,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    EnvLevelPolicy,
    InvalidLevelSnapshot,
    LevelController,
    LevelOverride,
    RuntimeLevelPolicy,
    get_installed_policy,
    order_categories,
    parse_env_levels,
    parse_snapshot,
    serialize_snapshot,
    snapshot_value,
)
from app.logging.security import emit_level_change_record
from app.logging.discovery import LoggingDiscoveryUnavailable
from app.models.system_setting import SystemSetting
from app.services.settings_service import _stage_setting

# Re-exported so callers can import the reserved keys from one place.
__all__ = [
    "LEVEL_VERSION_KEY",
    "LOGGING_LEVELS_KEY",
    "get_log_levels",
    "local_logger_categories",
    "publish_level_version",
    "read_level_snapshot",
    "resolve_offered_categories",
    "update_log_levels",
]

logger = logging.getLogger(__name__)


def _default_policy() -> RuntimeLevelPolicy:
    policy = get_installed_policy()
    if policy is not None:
        return policy
    return RuntimeLevelPolicy(parse_env_levels())


def local_logger_categories() -> set[str]:
    """Every category backed by a real ``logging.Logger`` in this process."""
    categories: set[str] = set()
    for name, obj in list(logging.Logger.manager.loggerDict.items()):
        if not isinstance(obj, logging.Logger):
            continue
        category = category_for(name)
        if is_valid_category(category):
            categories.add(category)
    return categories


async def _active_plugin_names(db) -> set[str] | None:
    """Names of ACTIVE plugin rows, or ``None`` when the table is unavailable.

    ``None`` means the discovery filter is skipped (e.g. a test schema that
    mirrors only settings/audit) — never a false "no plugins".

    The SELECT runs inside a SAVEPOINT so a failed lookup (e.g. an unavailable
    ``plugins`` relation) discards only this read. It must never roll back the
    caller's session: ``update_log_levels`` holds a transaction-scoped
    ``pg_advisory_xact_lock`` here, and a session-level ``rollback()`` would
    release that lock mid read-modify-write, breaking the T-299-03
    serialization invariant (CR-01). The savepoint (not merely "no rollback")
    is required because a real DB error leaves the outer transaction aborted
    and would fail the subsequent read.
    """
    try:
        from app.models.plugin import Plugin, PluginState

        async with db.begin_nested():
            result = await db.execute(
                select(Plugin.name).where(Plugin.state == PluginState.ACTIVE.value)
            )
            names = {name for (name,) in result.all()}
        return names
    except Exception:
        return None


async def _discovery_categories(discovery) -> set[str]:
    """Categories offered by the injected discovery (reports or plain callable)."""
    if discovery is None:
        return set()
    read_active = getattr(discovery, "read_active", None)
    if read_active is not None:
        reports = await read_active()
        offered: set[str] = set()
        for report in reports or ():
            for entry in getattr(report, "categories", ()) or ():
                category = getattr(entry, "category", None)
                if isinstance(category, str) and is_valid_category(category):
                    offered.add(category)
        return offered
    extra = discovery() if callable(discovery) else discovery
    if hasattr(extra, "__await__"):
        extra = await extra
    return {
        category
        for category in (extra or ())
        if isinstance(category, str) and is_valid_category(category)
    }


async def resolve_offered_categories(db, discovery: Callable | Iterable | None = None) -> list[str]:
    """The shared offer authority for GET and PUT (D-01, Pattern 1).

    Core categories are always offered. Dynamic categories come from local
    real loggers plus the injected discovery (DB leases or a plain callable);
    ``plugin.<name>`` candidates are narrowed to ACTIVE owners when the plugins
    table is readable. A discovery projection failure propagates as
    ``LoggingDiscoveryUnavailable`` (→ 503), never a partial success.
    """
    dynamic: set[str] = set(local_logger_categories())
    dynamic |= await _discovery_categories(discovery)

    dynamic -= set(CORE_CATEGORIES)

    active = await _active_plugin_names(db)
    if active is not None:
        dynamic = {
            category
            for category in dynamic
            if not category.startswith("plugin.")
            or category[len("plugin."):] in active
        }

    return order_categories(set(CORE_CATEGORIES) | dynamic)


async def read_level_snapshot(session_factory) -> dict[str, LevelOverride]:
    """Read + validate the persisted snapshot with its own short session."""
    async with session_factory() as db:
        result = await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == LOGGING_LEVELS_KEY)
        )
        raw = result.scalar_one_or_none()
    return parse_snapshot(raw)


async def _read_raw(db) -> str | None:
    result = await db.execute(
        select(SystemSetting.value).where(SystemSetting.key == LOGGING_LEVELS_KEY)
    )
    return result.scalar_one_or_none()


def _project_row(category: str, policy: RuntimeLevelPolicy, snapshot, now: datetime) -> dict:
    base_level = policy.base_level(category)
    override = snapshot.get(category)
    active = override is not None and override.is_active(now)
    if active:
        effective = override.level
        source = "override"
        remaining = (
            max(0, int((override.expires_at - now).total_seconds()))
            if override.expires_at is not None
            else None
        )
        return {
            "category": category,
            "base_level": base_level,
            "ttl_required_below": policy.ttl_threshold(category),
            "effective_level": effective,
            "source": source,
            "override_level": override.level,
            "expires_at": override.expires_at,
            "remaining_seconds": remaining,
        }
    return {
        "category": category,
        "base_level": base_level,
        "ttl_required_below": policy.ttl_threshold(category),
        "effective_level": base_level,
        "source": "env",
        "override_level": None,
        "expires_at": None,
        "remaining_seconds": None,
    }


async def get_log_levels(
    db,
    discovery: Callable | Iterable | None = None,
    policy: RuntimeLevelPolicy | None = None,
    now: datetime | None = None,
) -> dict:
    """Project fresh DB truth + env basis into the shared response envelope."""
    policy = policy or _default_policy()
    now = now or datetime.now(timezone.utc)
    try:
        snapshot = parse_snapshot(await _read_raw(db))
    except InvalidLevelSnapshot as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        categories = await resolve_offered_categories(db, discovery)
    except LoggingDiscoveryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "server_time": now,
        "categories": [_project_row(category, policy, snapshot, now) for category in categories],
    }


def _validate_entries(request, offered: set[str], policy: RuntimeLevelPolicy) -> list[tuple[str, Any]]:
    """Validate the whole map before any mutation (atomic 422)."""
    parsed: list[tuple[str, Any]] = []
    for category, entry in request.levels.items():
        if not is_valid_category(category) or category not in offered:
            raise HTTPException(
                status_code=422, detail=f"unknown log category {category!r}"
            )
        if entry is None:
            parsed.append((category, None))
            continue

        threshold = policy.ttl_threshold(category)
        level_value = LEVEL_VALUES[entry.level]
        ttl = entry.ttl_seconds
        if level_value < threshold and ttl is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"level {entry.level} for {category!r} is below the required "
                    f"threshold; ttl_seconds is required"
                ),
            )
        if ttl is not None and not (MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"ttl_seconds must be between {MIN_TTL_SECONDS} and "
                    f"{MAX_TTL_SECONDS}"
                ),
            )
        parsed.append((category, (entry.level, ttl)))
    return parsed


def _apply_entries(current, parsed, now):
    new_snapshot = dict(current)
    old_changed: dict[str, Any] = {}
    new_changed: dict[str, Any] = {}
    for category, value in parsed:
        previous = new_snapshot.get(category)
        if value is None:
            if category in new_snapshot:
                old_changed[category] = snapshot_value(previous)
                new_changed[category] = None
                del new_snapshot[category]
            continue
        level, ttl = value
        expires_at = now + timedelta(seconds=ttl) if ttl is not None else None
        candidate = LevelOverride(level=level, expires_at=expires_at)
        if previous is not None and previous.level == level and previous.expires_at == expires_at:
            continue
        old_changed[category] = snapshot_value(previous)
        new_changed[category] = snapshot_value(candidate)
        new_snapshot[category] = candidate
    return new_snapshot, old_changed, new_changed


async def update_log_levels(
    db,
    request,
    actor_id,
    controller: LevelController | None = None,
    discovery: Callable | Iterable | None = None,
    request_context: dict | None = None,
) -> dict:
    """Atomically merge/clear level overrides and apply them locally.

    Uses a transaction-scoped PostgreSQL advisory lock BEFORE the first read so
    two replicas (including concurrent first inserts) serialize (T-299-03).
    """
    policy = controller.policy if controller is not None else _default_policy()

    body = json.dumps(request.model_dump(), separators=(",", ":"))
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise HTTPException(status_code=422, detail="request body too large")

    # Serialize the whole read-modify-write for the reserved key.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": LOGGING_LEVELS_KEY},
    )

    try:
        offered = set(await resolve_offered_categories(db, discovery))
    except LoggingDiscoveryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    parsed = _validate_entries(request, offered, policy)

    try:
        current = parse_snapshot(await _read_raw(db))
    except InvalidLevelSnapshot as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # D-06/D-23: the server resolves the absolute expiry from ONE server clock
    # reading taken AFTER the lock was acquired (never the transport time). The
    # policy clock seam makes this deterministically observable in tests.
    now = policy.now()
    new_snapshot, old_changed, new_changed = _apply_entries(current, parsed, now)

    if old_changed:
        new_raw = serialize_snapshot(new_snapshot)
        await _stage_setting(db, LOGGING_LEVELS_KEY, new_raw, actor_id)
        await db.commit()

        if controller is not None:
            await controller.refresh_from_db()
        emit_level_change_record(old_changed, new_changed, actor_id, request_context)
        await publish_level_version()

    return await get_log_levels(db, discovery, policy, now)


async def publish_level_version() -> None:
    """Best-effort Valkey INCR — never turns a committed save into an error."""
    try:
        from app.db.redis import get_redis

        redis = await get_redis()
        await redis.incr(LEVEL_VERSION_KEY)
    except Exception:
        logger.debug("could not publish log level version", exc_info=True)
