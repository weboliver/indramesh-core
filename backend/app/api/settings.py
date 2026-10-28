from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.logging.levels import DISCOVERY_KEY_PREFIX, LOGGING_LEVELS_KEY
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.settings_service import get_all_settings, set_setting
from app.services.audit_service import set_history_enabled, HISTORY_SETTING_KEY
from app.services.password_policy_service import (
    PASSWORD_POLICY_SETTING,
    save_password_policy,
)
from app.services.object_file_service import (
    OBJECT_FILE_MAX_SIZE_MB_CAP,
    OBJECT_FILE_MAX_SIZE_MB_SETTING,
)

router = APIRouter(prefix="/settings", tags=["system-settings"])

OBJECT_FILE_LIMIT_ERROR = (
    f"object_file_max_size_mb must be a whole MB value between 1 and "
    f"{OBJECT_FILE_MAX_SIZE_MB_CAP}"
)


def _parse_whole_positive_mb(value) -> int:
    """Canonical integer for a whole positive MB setting input (D-06, T-279-12).

    Accepts int, integral float, and integer-valued string input; zero,
    negative, fractional, malformed, nonnumeric, and above-cap shapes get
    HTTP 422 and never reach persistence. Booleans are rejected explicitly —
    `int(True)` would otherwise parse as the valid value 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise HTTPException(status_code=422, detail=OBJECT_FILE_LIMIT_ERROR)
    if isinstance(value, float):
        if not value.is_integer():
            raise HTTPException(status_code=422, detail=OBJECT_FILE_LIMIT_ERROR)
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise HTTPException(status_code=422, detail=OBJECT_FILE_LIMIT_ERROR)
    if value < 1 or value > OBJECT_FILE_MAX_SIZE_MB_CAP:
        raise HTTPException(status_code=422, detail=OBJECT_FILE_LIMIT_ERROR)
    return value


@router.get("/")
async def list_settings(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("settings", "read"))
):
    return await get_all_settings(db)


@router.put("/{key}")
async def update_setting(
    key: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("settings", "update"))
):
    # D-08 / T-299-02: the logging-level keys have a dedicated, validated,
    # audited write path. The generic seam must never accept them — otherwise
    # an unvalidated JSON blob (no TTL, no security record) could be stored.
    if key == LOGGING_LEVELS_KEY or key.startswith(DISCOVERY_KEY_PREFIX):
        raise HTTPException(
            status_code=422,
            detail=(
                f"setting key {key!r} is reserved; use "
                "PUT /api/v1/logging/levels for logging-level changes"
            ),
        )

    value = body.get("value")
    if value is None:
        raise HTTPException(status_code=422, detail="value is required")

    if key == HISTORY_SETTING_KEY:
        enabled = str(value).lower() == "true"
        await set_history_enabled(db, enabled, current_user.id)
        return {"key": key, "value": enabled}

    if key == PASSWORD_POLICY_SETTING:
        try:
            policy = await save_password_policy(db, value, current_user.id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"key": key, "value": policy}

    if key == OBJECT_FILE_MAX_SIZE_MB_SETTING:
        mb = _parse_whole_positive_mb(value)
        await set_setting(db, key, str(mb), actor_id=current_user.id)
        return {"key": key, "value": mb}

    await set_setting(db, key, str(value), actor_id=current_user.id)
    return {"key": key, "value": value}
