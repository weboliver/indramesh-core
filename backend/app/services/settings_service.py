import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.system_setting import SystemSetting

from app.logging.levels import DISCOVERY_KEY_PREFIX, LOGGING_LEVELS_KEY


class ReservedSettingKeyError(ValueError):
    """A generic write touched a key owned by a dedicated service path.

    The logging-level keys (``logging.levels`` and the plan-02 discovery
    prefix) must only be written through the validated level service — the
    generic HTTP/service seam has no TTL/validation/security-record logic and
    would silently corrupt the contract (T-299-02).
    """


def _is_reserved_key(key: str) -> bool:
    return key == LOGGING_LEVELS_KEY or key.startswith(DISCOVERY_KEY_PREFIX)


def _settings_entity_id(key: str) -> uuid.UUID:
    """SystemSetting is keyed by a string, not a UUID — derive a stable UUID
    from the key so repeated changes to the same setting share one
    audit_logs.entity_id over time (deterministic, not random)."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"system_setting:{key}")


async def get_setting(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def get_all_settings(db: AsyncSession) -> dict[str, str]:
    result = await db.execute(select(SystemSetting))
    # Plan-02 process-discovery leases are internal rows; never expose them on
    # the generic settings list (they are not user-editable settings).
    return {
        s.key: s.value
        for s in result.scalars().all()
        if not s.key.startswith(DISCOVERY_KEY_PREFIX)
    }


async def _stage_setting(
    db: AsyncSession, key: str, value: str, actor_id: uuid.UUID | None = None
):
    """Upsert + audit for one setting WITHOUT committing (the level service's
    transactional seam). Returns the audit entry (or None when disabled)."""
    from app.services.audit_service import audit_log, model_to_dict

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    entity_id = _settings_entity_id(key)
    if setting:
        old_values = model_to_dict(setting)
        setting.value = value
        await db.flush()
        return await audit_log(db, "settings", entity_id, "UPDATE", actor_id,
                               old_values=old_values, new_values=model_to_dict(setting))
    setting = SystemSetting(key=key, value=value)
    db.add(setting)
    await db.flush()
    return await audit_log(db, "settings", entity_id, "CREATE", actor_id,
                           old_values=None, new_values=model_to_dict(setting))


async def set_setting(
    db: AsyncSession, key: str, value: str, actor_id: uuid.UUID | None = None
) -> None:
    if _is_reserved_key(key):
        raise ReservedSettingKeyError(
            f"setting key {key!r} is reserved for the dedicated logging-level service"
        )
    await _stage_setting(db, key, value, actor_id)
    await db.commit()
