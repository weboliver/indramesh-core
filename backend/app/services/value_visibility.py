"""The one "values visible to this principal" predicate (D-05).

Every value read path composes the clauses returned here instead of
re-implementing a record-permission check. The predicate has three parts:

1. the **object-level** record DENY (composed from
   ``record_permission_service.build_record_permission_filter``, cascade over
   object -> object_type -> schema -> source),
2. the **attribute-type-level** record DENY (the same builder, standalone
   single-level table),
3. ``object_type_attributes.is_hidden`` for a non-superuser.

A superuser gets an empty clause list and no query at all — the RLR-05 bypass
parity the existing routes already honour. A user holding no roles receives no
record-permission clause from the builder (the DENY-only model is permissive
without roles), so such a principal sees exactly what the attribute-type gate
allows; that is the intended reading, not a leak.

Callers must join the tables the clauses reference:
``objects`` (or ``object_alias``), ``object_type_attributes`` (or ``ota_alias``)
and, when the attribute-type clause is not ``None``, ``attribute_types``.

:func:`visible_attribute_conditions` is the name-space form of the same
predicate: it answers "may this principal name this attribute at all?" for
lookups that have no owning object row (AQL name resolution).
"""

import hashlib
import uuid
from collections.abc import Iterable

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Object, ObjectTypeAttribute
from app.services.record_permission_service import (
    _resolve_user_role_ids,
    build_record_permission_filter,
    resolve_record_permission,
)

ATTRIBUTE_TYPE_ALIAS = "attribute_types"


async def resolve_principal_roles(
    db: AsyncSession, user_id: str, is_superuser: bool
) -> list[uuid.UUID] | None:
    """Resolve the caller's effective roles **once** per request.

    ``None`` for a superuser (the RLR-05 bypass never needs them). Passing the
    returned list to every fragment keeps the two ``:role_ids`` bindparams in
    one statement on the same value (pitfall P-08) and halves the UNION queries.
    """
    if is_superuser:
        return None
    return await _resolve_user_role_ids(db, user_id)


def principal_signature(
    user_id: str,
    role_ids: list[uuid.UUID] | None,
    *,
    is_superuser: bool,
) -> str:
    """Cache-key component that makes a cached value list principal-scoped.

    A stable hash of the resolved role ids (plus the user id) — a literal
    ``"admin"``/``"user"`` string is not a principal dimension (pitfall P-02).
    """
    if is_superuser:
        return f"{user_id}:superuser"
    digest = hashlib.sha256(
        ",".join(sorted(str(role_id) for role_id in (role_ids or []))).encode()
    ).hexdigest()[:16]
    return f"{user_id}:{digest}"


def _hidden_clause(ota_alias: str):
    """``is_hidden`` for the joined OTA table, alias-aware."""
    if ota_alias == "object_type_attributes":
        return ObjectTypeAttribute.is_hidden.is_(False)
    return text(f"{ota_alias}.is_hidden IS FALSE")


async def visible_value_conditions(
    db: AsyncSession,
    user_id: str,
    is_superuser: bool,
    *,
    object_alias: str | None = None,
    ota_alias: str = "object_type_attributes",
    role_ids: list[uuid.UUID] | None = None,
) -> list:
    """WHERE-clauses restricting ObjectValue rows to what this principal may see.

    Superuser (RLR-05) -> ``[]`` — no clause and no database query.
    ``role_ids`` may be supplied by a caller that resolved the roles once.
    """
    if is_superuser:
        return []

    clauses: list = [_hidden_clause(ota_alias)]

    object_clause = await build_record_permission_filter(
        db,
        user_id,
        "object",
        is_superuser=False,
        alias=object_alias,
        role_ids=role_ids,
    )
    if object_clause is not None:
        clauses.append(object_clause)

    attribute_type_clause = await build_record_permission_filter(
        db,
        user_id,
        "attribute_type",
        is_superuser=False,
        alias=ATTRIBUTE_TYPE_ALIAS,
        role_ids=role_ids,
    )
    if attribute_type_clause is not None:
        clauses.append(attribute_type_clause)

    return clauses


async def visible_attribute_conditions(
    db: AsyncSession,
    user_id: str,
    is_superuser: bool,
    *,
    ota_alias: str = "object_type_attributes",
    role_ids: list[uuid.UUID] | None = None,
) -> list:
    """WHERE-clauses restricting ObjectTypeAttribute rows to names the caller may use.

    The **name-space** half of the same predicate (D-05): a name whose
    AttributeType carries a record DENY, or whose OTA row is ``is_hidden`` for a
    non-superuser, must be indistinguishable from a name that does not exist at
    all — otherwise a query-language name lookup answers "this exists" for data
    the caller may not read (AQL name resolution, SEC313-12/SEC313-19).

    Unlike :func:`visible_value_conditions` there is no object clause here: an
    ObjectTypeAttribute row has no owning object. The caller joins
    ``attribute_types`` (the attribute-type clause references it).
    """
    if is_superuser:
        return []

    clauses: list = [_hidden_clause(ota_alias)]

    attribute_type_clause = await build_record_permission_filter(
        db,
        user_id,
        "attribute_type",
        is_superuser=False,
        alias=ATTRIBUTE_TYPE_ALIAS,
        role_ids=role_ids,
    )
    if attribute_type_clause is not None:
        clauses.append(attribute_type_clause)

    return clauses


async def assert_record_action_allowed(
    user_id: str,
    is_superuser: bool,
    *,
    object_id: uuid.UUID | None = None,
    attribute_type_id: uuid.UUID | None = None,
    action: str,
) -> None:
    """403 when a record-level DENY blocks ``action`` on the object/attribute type.

    Single-row write gate: the existing ``resolve_record_permission`` resolver is
    the right seam here (a write checks one object, not a cached result set).
    """
    if is_superuser:
        return
    if object_id is not None:
        decision = await resolve_record_permission(user_id, "object", object_id, action)
        if decision is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied for this object",
            )
    if attribute_type_id is not None:
        decision = await resolve_record_permission(
            user_id, "attribute_type", attribute_type_id, action
        )
        if decision is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied for this attribute type",
            )


async def visible_object_ids_subquery(
    db: AsyncSession,
    user_id: str,
    is_superuser: bool,
    *,
    role_ids: list[uuid.UUID] | None = None,
):
    """``select(objects.id)`` narrowed by the object-level predicate.

    For ``IN (subquery)`` consumers (export row selection, AQL anchors, the
    system-binding synthesis guard). Superusers get the unfiltered select.
    """
    stmt = select(Object.id)
    if is_superuser:
        return stmt
    object_clause = await build_record_permission_filter(
        db, user_id, "object", is_superuser=False, role_ids=role_ids
    )
    if object_clause is not None:
        stmt = stmt.where(object_clause)
    return stmt


async def visible_object_id_set(
    db: AsyncSession,
    user_id: str,
    is_superuser: bool,
    object_ids: Iterable[uuid.UUID],
    *,
    role_ids: list[uuid.UUID] | None = None,
) -> set[uuid.UUID]:
    """Intersect a requested object id set with the visible one.

    Used by the value exits where a caller-supplied id set (``object_id`` /
    ``object_ids``) must not be handed to ``synthesize_system_bindings``
    unfiltered — a denied object would otherwise still yield its synthetic
    ``Name``/``Key`` (pitfall P-01).
    """
    ids = {object_id for object_id in object_ids if object_id is not None}
    if not ids or is_superuser:
        return ids
    subquery = await visible_object_ids_subquery(
        db, user_id, is_superuser=False, role_ids=role_ids
    )
    result = await db.execute(
        select(Object.id).where(Object.id.in_(ids), Object.id.in_(subquery))
    )
    return set(result.scalars().all())
