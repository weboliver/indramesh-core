"""Administrator-configurable password policy (Settings -> Password policy).

Stored as one JSON value under ``password_policy`` in ``system_settings`` so the
five fields are validated and saved together (min <= max holds atomically).
Checked when an administrator creates a user or a superuser resets a password,
and when a user changes their own password.

The maximum is capped at 72 bytes: bcrypt reads only the first 72 bytes of a
password and silently ignores the rest, so a longer limit would only pretend
to accept stronger passwords.
"""

import json

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

PASSWORD_POLICY_SETTING = "password_policy"
PASSWORD_MIN_LENGTH_FLOOR = 8
PASSWORD_MAX_BYTES = 72

DEFAULT_PASSWORD_POLICY = {
    "min_length": PASSWORD_MIN_LENGTH_FLOOR,
    "max_length": PASSWORD_MAX_BYTES,
    "require_digit": False,
    "require_special": False,
    "require_uppercase": False,
}


def _whole_number(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be a whole number")
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{field} must be a whole number") from None


def parse_password_policy(raw) -> dict:
    """Validate a submitted policy; raises ``ValueError`` naming the problem."""
    if not isinstance(raw, dict):
        raise ValueError("password_policy must be an object")
    unknown = set(raw) - set(DEFAULT_PASSWORD_POLICY)
    if unknown:
        raise ValueError(f"unknown password_policy fields: {', '.join(sorted(unknown))}")
    policy = {**DEFAULT_PASSWORD_POLICY, **raw}
    min_length = _whole_number(policy["min_length"], "min_length")
    max_length = _whole_number(policy["max_length"], "max_length")
    if not PASSWORD_MIN_LENGTH_FLOOR <= min_length <= PASSWORD_MAX_BYTES:
        raise ValueError(
            f"min_length must be between {PASSWORD_MIN_LENGTH_FLOOR} and {PASSWORD_MAX_BYTES}"
        )
    if not min_length <= max_length <= PASSWORD_MAX_BYTES:
        raise ValueError(f"max_length must be between min_length and {PASSWORD_MAX_BYTES}")
    for field in ("require_digit", "require_special", "require_uppercase"):
        if not isinstance(policy[field], bool):
            raise ValueError(f"{field} must be true or false")
    return {
        "min_length": min_length,
        "max_length": max_length,
        "require_digit": policy["require_digit"],
        "require_special": policy["require_special"],
        "require_uppercase": policy["require_uppercase"],
    }


async def get_password_policy(db: AsyncSession) -> dict:
    """The stored policy, or the default when none (or an unreadable one) is stored."""
    raw = await get_setting(db, PASSWORD_POLICY_SETTING)
    if raw is None:
        return dict(DEFAULT_PASSWORD_POLICY)
    try:
        return parse_password_policy(json.loads(raw))
    except (ValueError, TypeError):
        return dict(DEFAULT_PASSWORD_POLICY)


async def save_password_policy(db: AsyncSession, raw, actor_id) -> dict:
    policy = parse_password_policy(raw)
    await set_setting(db, PASSWORD_POLICY_SETTING, json.dumps(policy), actor_id=actor_id)
    return policy


def password_violations(password: str, policy: dict) -> list[str]:
    violations = []
    if len(password) < policy["min_length"]:
        violations.append(f"at least {policy['min_length']} characters")
    if len(password) > policy["max_length"]:
        violations.append(f"at most {policy['max_length']} characters")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        violations.append(f"at most {PASSWORD_MAX_BYTES} bytes")
    if policy["require_digit"] and not any(ch.isdigit() for ch in password):
        violations.append("a digit")
    if policy["require_uppercase"] and not any(ch.isupper() for ch in password):
        violations.append("an uppercase letter")
    if policy["require_special"] and all(ch.isalnum() for ch in password):
        violations.append("a special character")
    return violations


async def enforce_password_policy(db: AsyncSession, password: str) -> None:
    violations = password_violations(password, await get_password_policy(db))
    if violations:
        raise HTTPException(
            status_code=422,
            detail="Password needs " + ", ".join(violations) + ".",
        )
