"""Audit service for entity CRUD change tracking.

Provides the SENSITIVE_AUDIT_FIELDS blocklist, compute_diff() utility,
and the audit_log() async function called explicitly at each CREATE /
UPDATE / DELETE mutation site in service-layer functions.

Usage:
    from app.services.audit_service import audit_log, compute_diff

    # After CREATE (all values new, old=None):
    await audit_log(db, "schemas", schema.id, "CREATE", current_user.id,
                    old_values=None, new_values=asdict(schema))

    # Before UPDATE commit (only changed fields):
    diff = compute_diff(old_values, new_values)
    await audit_log(db, "schemas", schema.id, "UPDATE", current_user.id,
                    old_values=old_values, new_values=new_values)

    # Before DELETE (full snapshot, new=None):
    old_values = model_to_dict(obj)
    await audit_log(db, "schemas", schema.id, "DELETE", current_user.id,
                    old_values=old_values, new_values=None)
    await db.delete(obj)
"""

import uuid
from enum import Enum
from functools import wraps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.db.redis import get_redis
from app.models.audit_log import AuditLog
from app.models.audit_log_config import AuditLogConfig


class AuditEntityType(str, Enum):
    """Single source of truth for the 16 auditable entity type values.
    Defined here per D-17 — close to where both audit_log() and config
    service functions consume it.

    Values are plain snake_case, matching the literal strings already
    passed as the first positional arg to every audit_log(db, "...", ...)
    call site wired since Phase 163 (verified via
    `grep -rhoP 'audit_log\\(db, "\\K[a-z_]+' backend/app/services/*.py`).
    Per 166-PLAN-REVIEW.md #3: hyphenated values would never match a row
    actually written to audit_logs.entity_type.
    """
    SOURCES = "sources"
    SCHEMAS = "schemas"
    OBJECT_TYPES = "object_types"
    OBJECTS = "objects"
    OBJECT_VALUES = "object_values"
    OBJECT_REFS = "object_refs"
    ATTRIBUTE_TYPES = "attribute_types"
    REFERENCE_TYPES = "reference_types"
    STATUSES = "statuses"
    ICONS = "icons"
    TARGETS = "targets"
    SERVICE_ACCOUNTS = "service_accounts"
    API_CLIENTS = "api_clients"
    # NOTE: the following 3 have NO existing audit_log() call site anywhere
    # in the codebase yet (verified — no service file calls audit_log() for
    # these entity types). Listing them here reserves the config slot, but
    # toggling them "on" has zero effect until a follow-up phase wires their
    # CRUD service functions with audit_log() calls.
    SCHEMA_TEMPLATES = "schema_templates"
    IMPORT_DEFINITIONS = "import_definitions"
    EXPORT_DEFINITIONS = "export_definitions"
    TASKS = "tasks"
    SAVED_QUERIES = "saved_queries"
    # Phase 235 / D-17: development-only filesystem export of resolved
    # systemui dictionaries — security-relevant (writes outside the DB) so
    # it is always-on (ALWAYS_ON_ENTITY_TYPES below), like api_clients.
    UI_TRANSLATION_EXPORTS = "ui_translation_exports"
    # Security/config-critical entity types — always audited, toggle locked on
    # (ALWAYS_ON_ENTITY_TYPES below). Unlike the other 15, seeded with
    # audit_log_enabled=True by default (seed_audit_log_config).
    AUDIT_LOG_CONFIG = "audit_log_config"
    SETTINGS = "settings"
    PERMISSIONS = "permissions"
    USERS = "users"
    ROLES = "roles"
    PLUGINS = "plugins"


# Entity types whose audit toggle can never be switched off via the
# audit-log-config API — security/config-critical changes (who has what
# access, what audit settings and system settings are in effect) must always
# leave a trail. Enforced in app/api/audit_log_config.py's PATCH route.
# WR-03: api_clients joined the set (Plan 200-05) — credential lifecycle
# (create/update/revoke) is security-critical and must always be audited.
ALWAYS_ON_ENTITY_TYPES: frozenset[str] = frozenset({
    AuditEntityType.AUDIT_LOG_CONFIG.value,
    AuditEntityType.SETTINGS.value,
    AuditEntityType.PERMISSIONS.value,
    AuditEntityType.USERS.value,
    AuditEntityType.ROLES.value,
    AuditEntityType.API_CLIENTS.value,
    AuditEntityType.UI_TRANSLATION_EXPORTS.value,
    AuditEntityType.PLUGINS.value,
})


# Module-level set of field names that MUST be excluded from audit diff
# output. Derived from app/logging/redaction.py's _SENSITIVE_PATTERNS regex
# field names, plus created_at/updated_at for noise reduction per success
# criterion 4.
# Per D-05: applied as an exclusion filter during diff computation — any
# field whose name matches is excluded from the changes dict entirely.
SENSITIVE_AUDIT_FIELDS: set[str] = {
    # Credential fields (from app/logging/redaction.py _SENSITIVE_PATTERNS)
    "source_bearer_token",
    "target_bearer_token",
    "token_hash",
    "password",
    "password_hash",
    "token",
    "bearer",
    "authorization",
    "api_key",
    "JWT_SECRET_KEY",
    "FERNET_KEY",
    # Noise-reduction timestamps (success criterion 4)
    "created_at",
    "updated_at",
}


def compute_diff(
    old_values: dict | None,
    new_values: dict | None,
    exclude_fields: set[str] | None = None,
) -> dict:
    """Compute a field-level diff dict between old and new values.

    Per D-02, returns:
        {"field_name": {"old": <value_before>, "new": <value_after>}}

    Rules:
    - If old_values is None (CREATE): all new_values keys appear with
      old=null.
    - If new_values is None (DELETE): all old_values keys appear with
      new=null.
    - If both are provided (UPDATE): only keys whose old != new appear.
    - Fields in exclude_fields are omitted from the result entirely.
    - created_at and updated_at are always excluded (included in
      SENSITIVE_AUDIT_FIELDS by default).

    Args:
        old_values: Dict of field values before the mutation (None for CREATE).
        new_values: Dict of field values after the mutation (None for DELETE).
        exclude_fields: Set of field names to exclude. Defaults to
                        SENSITIVE_AUDIT_FIELDS.

    Returns:
        Dict with field-level old/new mappings.
    """
    if exclude_fields is None:
        exclude_fields = SENSITIVE_AUDIT_FIELDS

    changes: dict = {}

    if old_values is None and new_values is not None:
        # CREATE: all new values with old=null (including fields whose
        # initial value is None/falsy — every non-excluded field is
        # recorded, unconditionally)
        for field, value in new_values.items():
            if field in exclude_fields:
                continue
            changes[field] = {"old": None, "new": _serialize_for_json(value)}

    elif new_values is None and old_values is not None:
        # DELETE: all old values with new=null (D-03: full snapshot before delete)
        for field, value in old_values.items():
            if field in exclude_fields:
                continue
            old = _serialize_for_json(value)
            changes[field] = {"old": old, "new": None}

    elif old_values is not None and new_values is not None:
        # UPDATE: only fields that actually changed
        all_fields = set(old_values.keys()) | set(new_values.keys())
        for field in all_fields:
            if field in exclude_fields:
                continue
            old = old_values.get(field)
            new = new_values.get(field)
            if old != new:
                changes[field] = {
                    "old": _serialize_for_json(old),
                    "new": _serialize_for_json(new),
                }

    return changes


def _serialize_for_json(value: object) -> object:
    """Convert a value to a JSON-safe representation.

    Handles UUID -> str, datetime -> isoformat, and
    leaves basic types (str, int, bool, None, list, dict) as-is.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            return str(value)
    return value


async def audit_log(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None,
    old_values: dict | None = None,
    new_values: dict | None = None,
    object_id: uuid.UUID | None = None,
    object_type_attribute_id: uuid.UUID | None = None,
) -> AuditLog | None:
    """Create an audit log entry for an entity CRUD operation.

    Checks the per-entity-type audit toggle first — if audit is disabled
    for this entity_type, returns None without writing.

    Computes a field-level diff from old_values and new_values, excluding
    sensitive fields (SENSITIVE_AUDIT_FIELDS), and writes a single
    AuditLog row in the current DB transaction.

    Per D-01: this function is called explicitly at each mutation site,
    NOT via SQLAlchemy event listeners.
    Per D-03: for DELETE operations, call BEFORE db.delete(obj).
    Per D-04: actor_id is an explicit parameter (no ContextVar).

    Args:
        db: Active async DB session (same transaction as the mutation).
        entity_type: String identifying the entity type, using the model's
                     __tablename__ value (e.g., "schemas", "objects").
        entity_id: UUID of the entity being mutated.
        action: One of "CREATE", "UPDATE", "DELETE".
        actor_id: UUID of the acting user, or None for system operations.
        old_values: Dict of field values before mutation (None for CREATE).
        new_values: Dict of field values after mutation (None for DELETE).
        object_id: The owning Object's id, for entity types scoped to a
                   single Object (objects/object_values/object_refs) — lets
                   a per-object history view filter with one column across
                   all three entity types. None for entity types with no
                   owning Object (schemas, statuses, etc.).
        object_type_attribute_id: The attribute an object_values change
                   belongs to — lets a per-object history view resolve and
                   display the attribute's name instead of raw column names
                   like value_simple/value_struct. None for other entity
                   types.

    Returns:
        The newly created AuditLog ORM instance, or None if audit toggle
        is disabled for this entity_type.
    """
    if not await get_audit_log_config_cached(entity_type):
        return None

    changes = compute_diff(old_values, new_values)

    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        object_id=object_id,
        object_type_attribute_id=object_type_attribute_id,
        action=action,
        actor_id=actor_id,
        changes=changes,
    )
    db.add(entry)
    await db.flush()
    return entry


def model_to_dict(obj) -> dict:
    """Convert a SQLAlchemy model instance to a flat field dict for audit diff.

    Returns a dict mapping each column name to its current ORM-tracked value.
    Relationship attributes are NOT included — only actual DB columns.
    UUID values remain as uuid.UUID (serialized by _serialize_for_json in compute_diff).
    """
    return {
        c.key: getattr(obj, c.key)
        for c in obj.__table__.columns
    }


def resolve_select_option_display(payload: dict, structure: dict | None) -> dict:
    """Resolve SELECT option ids in an object_value audit payload to base
    display values (human-facing audit readability — CONTEXT discretion:
    resolve; T-223-19 accepted degradation on misses).

    Best-effort with raw fallback (D-15): a missing/unknown structure, a
    non-UUID stored value, or a deleted-option uuid all pass through
    unchanged — the audit diff never crashes and never fabricates text.
    Returns a NEW dict (the caller's payload is not mutated).
    """
    from app.services.select_option_service import build_option_map, resolve_display_value

    value = payload.get("value_simple")
    if not value or not structure:
        return dict(payload)
    out = dict(payload)
    out["value_simple"] = resolve_display_value(value, build_option_map(structure))
    return out


# ── AuditLogConfig service functions (Phase 166) ──

# Cache TTL for audit-log-config — long TTL per D-12 (config rarely changes)
AUDIT_CONFIG_CACHE_TTL: int = 1800  # 30 minutes


def _audit_config_cache_key(entity_type: str) -> str:
    """Redis key for a single entity type's audit toggle value.
    Pattern per D-09: audit-log-config:{entity_type}
    """
    return f"audit-log-config:{entity_type}"


def _audit_log_config_entity_id(entity_type: str) -> uuid.UUID:
    """AuditLogConfig is keyed by entity_type (a string), not a UUID — derive
    a stable UUID so repeated toggles of the same entity_type share one
    audit_logs.entity_id over time (deterministic, not random)."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"audit_log_config:{entity_type}")


async def get_audit_log_config_cached(entity_type: str) -> bool:
    """Get audit toggle value with lazy-populated Redis cache (D-10).
    Returns False (audit disabled) as default if no config row exists.

    Cache key: audit-log-config:{entity_type} — stores "true"/"false" string.
    Long TTL per D-12: 1800s (30 min).
    """
    if entity_type in ALWAYS_ON_ENTITY_TYPES:
        return True

    cache_key = _audit_config_cache_key(entity_type)

    # Try cache first (lazy-populate per D-10)
    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached == "true"
    except Exception:
        pass  # Cache miss or Redis down — fall through to DB

    # Cache miss — read from DB and populate cache
    async with async_session_maker() as db:
        result = await db.execute(
            select(AuditLogConfig).where(AuditLogConfig.entity_type == entity_type)
        )
        config = result.scalar_one_or_none()
        enabled = config.audit_log_enabled if config else False

    # Populate cache (best-effort)
    try:
        redis = await get_redis()
        await redis.setex(cache_key, AUDIT_CONFIG_CACHE_TTL, "true" if enabled else "false")
    except Exception:
        pass

    return enabled


async def list_audit_log_configs(db: AsyncSession) -> list[AuditLogConfig]:
    """Get all audit log config rows (D-03: return all, no per-type query needed)."""
    result = await db.execute(
        select(AuditLogConfig).order_by(AuditLogConfig.entity_type)
    )
    return list(result.scalars().all())


async def toggle_audit_log_config(
    db: AsyncSession,
    entity_type: str,
    enabled: bool,
    updated_by: uuid.UUID | None = None,
) -> AuditLogConfig | None:
    """Toggle audit_log_enabled for a given entity_type.
    Invalidates Redis cache inline per D-11.
    Returns the updated config row, or None if entity_type not found.
    """
    result = await db.execute(
        select(AuditLogConfig).where(AuditLogConfig.entity_type == entity_type)
    )
    config = result.scalar_one_or_none()
    if not config:
        return None

    old_values = model_to_dict(config)
    config.audit_log_enabled = enabled
    config.updated_by = updated_by
    # updated_at is auto-set by onupdate=datetime.now(timezone.utc).replace(tzinfo=None)
    await db.flush()
    await audit_log(db, "audit_log_config", _audit_log_config_entity_id(entity_type), "UPDATE",
                    updated_by, old_values=old_values, new_values=model_to_dict(config))
    await db.commit()

    # Inline cache invalidation per D-11
    try:
        redis = await get_redis()
        cache_key = _audit_config_cache_key(entity_type)
        await redis.delete(cache_key)
    except Exception:
        pass  # Cache invalidation failure is non-fatal

    return config


# ── "History" master switch (Objects + ObjectValues + ObjectRefs) ──

# The three entity types that together make up an object's change history.
# "History" is a derived on/off switch over these three audit toggles, not
# a fourth independent one — enabling it force-enables (and locks) all
# three; disabling it turns all three back off and lifts the lock.
HISTORY_ENTITY_TYPES: tuple[str, ...] = (
    AuditEntityType.OBJECTS.value,
    AuditEntityType.OBJECT_VALUES.value,
    AuditEntityType.OBJECT_REFS.value,
)

HISTORY_SETTING_KEY = "history_enabled"


async def set_history_enabled(
    db: AsyncSession, enabled: bool, updated_by: uuid.UUID | None = None
) -> None:
    """Force objects/object_values/object_refs audit logging on or off together
    and persist the History on/off state in system_settings.

    Per the user's spec: while History is on, the three underlying audit
    toggles cannot be switched off individually (enforced in the
    audit-log-config PATCH route) — turning History off is what switches
    them off again.
    """
    from app.services.settings_service import set_setting

    for entity_type in HISTORY_ENTITY_TYPES:
        await toggle_audit_log_config(db, entity_type, enabled, updated_by)
    await set_setting(db, HISTORY_SETTING_KEY, "true" if enabled else "false", actor_id=updated_by)


async def get_history_enabled(db: AsyncSession) -> bool:
    from app.services.settings_service import get_setting

    return await get_setting(db, HISTORY_SETTING_KEY) == "true"


# ── System user identity for worker audit context (Phase 166, AUDIT-07) ──

# Module-level cache for the system user ID — resolved once at first call.
# Per D-13: resolved at app startup (lazy on first access), stored in
# module-level variable, passed through @with_audit_context decorator.
_system_user_id: uuid.UUID | None = None
_system_user_resolved: bool = False


async def _resolve_system_user_id() -> uuid.UUID | None:
    """Resolve the admin user ID for worker audit attribution.
    Per D-14: the existing admin user (seeded in init_service.py) serves
    as the system identity. Resolved once, cached in module-level variable.
    """
    global _system_user_id, _system_user_resolved
    if _system_user_resolved:
        return _system_user_id

    from app.db import async_session_maker
    from app.models.user import User
    from sqlalchemy import select

    async with async_session_maker() as session:
        result = await session.execute(
            select(User.id).where(User.username == "admin")
        )
        _system_user_id = result.scalar_one_or_none()
        _system_user_resolved = True
        return _system_user_id


def with_audit_context(func):
    """Decorator that makes the system user ID available to ARQ job functions.
    Per D-15: standalone decorator wrapping ARQ job functions. Passes the
    system user ID to the job context so audit_log() calls within the job
    attribute the audit entry to a real user, not null.

    The decorator adds 'system_user_id' key to the ARQ ctx dict before
    calling the wrapped function. This value is then read by the job
    function and passed as actor_id to audit_log() calls.

    Per D-13: the system user ID is resolved lazily on first decorator
    invocation (not at app startup — init_service may not have run yet
    when the module imports).

    Usage:
        @with_audit_context
        async def my_job(ctx, task_id, ...):
            audit_log(db, ..., actor_id=ctx["system_user_id"], ...)
    """
    @wraps(func)
    async def wrapper(ctx: dict, *args, **kwargs):
        if "system_user_id" not in ctx:
            user_id = await _resolve_system_user_id()
            ctx["system_user_id"] = user_id
        return await func(ctx, *args, **kwargs)
    return wrapper
