"""Public object write endpoints (Phase 203).

This module hosts the Pydantic write wire contracts (ObjectIn, AttributeEntry,
ObjectAttributeValue) and the POST /object/create route handler. Plans 04/05
append the PUT /object/{id} and DELETE /object/{id} handlers to this same
module.

The wire shape is the EXACT body probe_jira_contract.py::capture_temp_object
sends to live Jira:

    {
        "objectTypeId": "<UUID>",
        "attributes": [
            {
                "objectTypeAttributeId": "<UUID>",
                "objectAttributeValues": [{"value": <native json>}, ...]
            },
            ...
        ]
    }

D-07: every value is {"value": ...} — scalars as native JSON, STATUS as a
local IndraMesh status UUID, REFERENCE as a local IndraMesh object UUID. The GET
wrappers ({"status": {...}}, {"referencedObject": {...}}) are NOT accepted on
input.
D-08: status ids and reference targets are strictly LOCAL IndraMesh UUIDs —
the public write contract is native identity only, with no foreign object id.
D-09: value stays native JSON (str/int/bool/None) — no envelope-level string
coercion; semantic type checks happen in the validation pass, not here.

WRIT-01: POST /object/create returns 201 + the full assembled Jira Object
shape (build_object_shape), assembled FRESH (never from cache). WRIT-04: the
handler delegates 100% of persistence to the shared services
(create_object + create_object_value + create_object_ref) — no raw inserts
(no `ObjectValue(`, `ObjectRef(`, or `db.add(` in this file). D-18..D-23:
post-commit, best-effort public cache purge (with include_hierarchy=True for
create — object/objecttype/schema count keys all change), then a D-23 seed of
the fresh shape into the calling service user's cache slot. Responses are
record-filtered for the caller (record_filter_object_shape — a DENY'd
reference target never renders back); the D-23 seed stays unfiltered
(quick 261012-blo). Reference targets the caller may not read are rejected
exactly like nonexistent ones (quick 261012-ca3); the response filter stays as
defense in depth.

Module-load order is load-bearing: the schemas below MUST be defined BEFORE
the `from app.public_api.object_write_service import ...` block at the bottom
of this file. `object_write_service.py` imports `ObjectIn` from this module
(Plan 02 contract), so a top-of-file import would create a circular
import — when `object_write` is being loaded, `object_write_service` is
loaded as a side-effect, and ITS import of `ObjectIn` runs before
`ObjectIn` is defined here. Defining the schemas first lets the
`object_write_service` import resolve cleanly.
"""

import uuid
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Object
from app.models.index_outbox import IndexOutboxStatus
from app.schemas.auth import UserResponse
from app.schemas.crud import ObjectCreate
from app.services.auth_service import (
    require_public_object_create,
    require_public_object_write,
)
from app.services.cache_service import invalidate_entity_cache
from app.services.hook_origin import user_origin
from app.services.object_ref_service import create_object_ref
from app.services.object_service import (
    _enqueue_index_job,
    _new_outbox_entry,
    _touch_system_attribute,
    create_object,
    delete_object,
)
from app.services.object_value_service import create_object_value
from app.services.value_lock_service import ValueLockConflict

# CORESEAM-11/D-14: the Core-owned origin names for the public write verbs.
# The public consumer is a distinct authenticated actor (the synthetic service
# user resolved by the permission dependency), never a plugin: every delegated
# create/delete write reports a `kind="user"` origin under these stable names
# instead of the internal service default (`object.create`/`object.delete`).
# A client payload can never choose these (T-291.2-13).
PUBLIC_OBJECT_CREATE_ORIGIN = "public-api.object-create"
PUBLIC_OBJECT_DELETE_ORIGIN = "public-api.object-delete"


class ObjectAttributeValue(BaseModel):
    # D-09: value stays native JSON (str/int/bool/None) — no envelope-level
    # string coercion. Type checking happens in the validation pass
    # (object_write_service.validate_object_payload), not here.
    value: Any


class AttributeEntry(BaseModel):
    # D-08: IndraMesh local UUID — NOT the Jira integer objectTypeAttributeId
    # from the GET response shape.
    objectTypeAttributeId: uuid.UUID
    # D-05/D-07: multi-value = one entry with N values.
    objectAttributeValues: List[ObjectAttributeValue]


class ObjectIn(BaseModel):
    # D-08: IndraMesh local UUID — the native object type identity.
    objectTypeId: uuid.UUID
    attributes: List[AttributeEntry]


# Imports that cross the object_write ↔ object_write_service boundary. They
# MUST be placed AFTER the schema class definitions above: object_write_service
# imports ObjectIn from this module (Plan 02 contract), so importing it
# earlier would create a circular import (see module docstring).
from app.public_api.object_read_service import (
    build_public_object_cache_key,
    cache_public_object_shape,
    load_object_read,
    record_filter_object_shape,
)
from app.public_api.object_shape import build_object_shape
from app.public_api.object_write_service import (
    assert_object_delete_attribute_types_allowed,
    purge_public_object_cache,
    replace_object_values,
    validate_object_payload,
)


# Plan 03/04/05 share this router — each plan appends one route. The prefix
# and tag match the read-side objects_router (public_api/objects.py:34) so the
# OpenAPI listing groups the read + write routes under one tag.
object_write_router = APIRouter(prefix="/object", tags=["public-object"])


# D-16: no per-route rate-limit decorator — the global SlowAPI
# middleware (main.py) enforces the per-token bucket on this route.
@object_write_router.post("/create", status_code=status.HTTP_201_CREATED)
async def public_create_object(
    payload: ObjectIn,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_object_create()),
):
    # D-01: fail-fast pre-validation BEFORE any shared-service call. The
    # validation pass resolves every objectTypeAttributeId, enforces data
    # types, status options, reference targets, cardinality, label presence,
    # and read-only attributes — all-or-nothing (Plan 02 contract).
    # 290 D-02/D-06: preflight_locks adds the persisted effective-lock check
    # over every submitted field AND the implicit user-supplied Name mirror,
    # so a real locked write answers 409 before create_object commits.
    plan = await validate_object_payload(
        db, payload, payload.objectTypeId, preflight_locks=True,
        user_id=str(current_user.id), is_superuser=current_user.is_superuser,
    )
    # WRIT-04: actor = the synthetic service user (resolve_public_api_consumer
    # in auth_service.py), so every audit row + outbox entry carries the
    # service user, not the admin (D-07).
    actor_id = uuid.UUID(str(current_user.id))
    # CORESEAM-11/D-14: one trusted public-consumer origin for every delegated
    # write in this composite request — create_object, each create_object_value
    # and each create_object_ref. The name is Core-owned; the DTO carries no
    # origin field, so a client can never impersonate a plugin/import origin.
    create_origin = user_origin(PUBLIC_OBJECT_CREATE_ORIGIN)

    # WRIT-04: one unit of work per object (D-15). Every delegated write runs
    # with commit=False / emit_index_outbox=False / invalidate_cache=False, so
    # the object, its values, its references and the per-value audit rows all
    # ride ONE transaction, and the write produces exactly one outbox row and
    # one cache purge instead of one per value (CR314-49 / EXT-06-T2).
    #
    # 290 D-04/D-06: a marker flipped between the preflight and the first
    # commit cannot be seen by the preflight — the shared services' own
    # pre-write guards raise ValueLockConflict before persisting their rows,
    # and the route maps it to the same 409 {"errors": [...]} contract
    # instead of letting it escape as a 500.
    try:
        obj = await create_object(
            db,
            ObjectCreate(
                schema_id=plan.schema_id,
                object_type_id=payload.objectTypeId,
                name=plan.label_value or "untitled",  # D-04: label drives name
            ),
            actor_id=actor_id,
            origin=create_origin,
            commit=False,
            emit_index_outbox=False,
            invalidate_cache=False,
        )
        if obj is None:
            # Plan 02 resolves the object type + schema; reaching None here means
            # the shared service failed to find the schema/object_type (a race
            # with a concurrent delete). Generic 500 — no internal detail leaks
            # on the public surface.
            raise HTTPException(status_code=500, detail="Object creation failed")

        # D-10/D-11: per-value shared-service calls in payload order. The
        # validation pass pre-populates object_id (placeholder) + the
        # from_object_id / from_attribute_name / source_id for ref ops; the
        # handler overwrites the from_object_id (the actual Object.id, known
        # only after create_object returns) and the object_type_attribute_id
        # (resolved OTA id, known at validation time). The audit trail stays
        # per value (D-10) — only the transaction boundary moved.
        for entry in plan.entries:
            for vop in entry.value_ops:
                # Validation already filled object_type_attribute_id; only the
                # real object_id is filled here.
                vop.object_id = obj.id
                await create_object_value(
                    db, vop, actor_id, origin=create_origin,
                    commit=False, emit_index_outbox=False, invalidate_cache=False,
                )
            for rop in entry.ref_ops:
                rop.from_object_id = obj.id
                rop.from_attribute_name = entry.attr_def["name"]
                await create_object_ref(
                    db, rop, actor_id, origin=create_origin,
                    commit=False, emit_index_outbox=False, invalidate_cache=False,
                )

        # D-15: ONE "Updated" touch for the whole payload, never one per value
        # (the per-value services drop theirs on the commit=False path). Keeps
        # the created object's wire shape — it carries both native timestamps
        # like an internal create does.
        await _touch_system_attribute(
            db, obj.id, obj.object_type_id, "Updated", commit=False
        )

        # ONE object-level outbox row, written INSIDE the data transaction and
        # pre-marked ENQUEUED: the enqueue below must not switch it to
        # ENQUEUED with its own UPDATE + commit (that would be a second
        # commit). This stays safe on an enqueue failure because
        # outbox_sweeper_job reaps rows whose status is PENDING *or* ENQUEUED
        # after the two-minute cooldown.
        outbox_entry = _new_outbox_entry(
            obj.id, obj.object_type_id, "index",
            actor_id=actor_id, origin=create_origin,
        )
        outbox_entry.status = IndexOutboxStatus.ENQUEUED.value
        db.add(outbox_entry)
        await db.commit()
    except ValueLockConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"errors": [str(exc)]})
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        # Same 4xx envelope the lock guard uses: a shared-service ValueError
        # (e.g. an abstract ObjectType) is a client error, never a 500.
        await db.rollback()
        raise HTTPException(status_code=400, detail={"errors": [str(exc)]})
    except Exception:
        await db.rollback()
        raise

    # Post-commit: one enqueue for the one outbox row (persist_status=False —
    # the row is already ENQUEUED), then one invalidation per namespace (not
    # one per value).
    await _enqueue_index_job(
        db, obj.id, "index_object_job",
        outbox_id=outbox_entry.id, persist_status=False,
    )
    await invalidate_entity_cache("object")
    await invalidate_entity_cache("search")

    # D-18..D-21: post-commit, best-effort public cache purge. Create
    # changes object/objecttype/schema count keys, so include_hierarchy=True.
    # Fail-open: the helper swallows all errors and the 60s public TTL
    # bounds worst-case staleness.
    await purge_public_object_cache(
        str(obj.id), str(obj.object_type_id), str(plan.schema_id),
        include_hierarchy=True,
    )

    # D-06: FRESH assembly — never read from cache (the shape was not
    # cached before the write, and pre-commit assembly would miss the new
    # values). D-23: seed the fresh shape into the shared cache slot (D-09:
    # the per-object key carries no user id) so the first consumer GET right
    # after this create is a hit for every caller.
    data = await load_object_read(db, obj.id)
    shape = build_object_shape(data)
    key = build_public_object_cache_key(str(obj.id), None)
    await cache_public_object_shape(key, shape, ttl=60)  # fail-open
    # quick 261012-blo: filter the caller's response AFTER seeding the shared
    # (caller-independent, unfiltered) entry.
    return await record_filter_object_shape(
        db, shape, obj.id, str(current_user.id), is_superuser=current_user.is_superuser
    )


# Plan 04: PUT /public/api/v1/object/{id} — Jira replace semantics (WRIT-02).
# D-16: no per-route rate-limit decorator — the global middleware enforces.
@object_write_router.put("/{object_id}", status_code=status.HTTP_200_OK)
async def public_replace_object(
    object_id: uuid.UUID,
    payload: ObjectIn,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_object_write("update")),
):
    # Existence + type-change guard. The permission dep chain already
    # produced 401/403/404 (D-14 invisible DENY) for auth failures; this
    # existence check is the legitimate 404 path for a nonexistent id.
    # The record-level DENY → 404 inside the dep returns the SAME detail
    # string ("Object not found"), so DENY and nonexistent are
    # indistinguishable to the consumer (no existence leak).
    obj = (
        await db.execute(
            select(Object).where(Object.id == object_id)
        )
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="Object not found")
    # objectTypeId change is REJECTED (discretion decision: attribute sets
    # differ between types — replace semantics become undefined if the
    # passed attributes target a different type's schema). 400 fires AFTER
    # the permission dep (401/403/404 beats 400 — D-15 ordering preserved).
    if payload.objectTypeId != obj.object_type_id:
        raise HTTPException(
            status_code=400, detail="objectTypeId change is not supported"
        )

    actor_id = uuid.UUID(str(current_user.id))
    # Captured BEFORE the write: replace_object_values rolls the session back
    # on failure, which expires ``obj`` — the finally-purge below must not
    # touch an expired ORM instance (a lazy refresh is not awaitable there).
    object_id_str = str(object_id)
    object_type_id_str = str(obj.object_type_id)
    schema_id_str = str(obj.schema_id)
    # WRIT-02: replace semantics — passed attributes fully replaced via
    # delete-then-recreate through the shared value services, unpassed
    # attributes keep their values, empty-attribute set padded with the
    # label attribute (handled inside replace_object_values). The final
    # update_object(...) call fires the Updated system attribute + outbox
    # + audit (WRIT-04). The object name is kept in sync when the label
    # attribute is among the entries (label_passed check).
    # 290 D-02/D-06: replace_object_values raises the single 409 with the
    # public errors list before the first commit; a marker flipped after that
    # preflight is still denied by the shared services' pre-write guards and
    # mapped here to the same 409 contract (residual race accepted).
    #
    # 317 EXT-06-T2: the service runs the whole delete/recreate sequence (plus
    # the object row lock and its single commit) as one transaction, so a
    # failure leaves the object exactly as it was. The public cache purge moved
    # into the `finally`: the error path must not keep a stale public shape for
    # the 60s TTL either — that gap is part of the measured finding.
    try:
        await replace_object_values(
            db, obj, payload, actor_id, is_superuser=current_user.is_superuser
        )
    except ValueLockConflict as exc:
        raise HTTPException(status_code=409, detail={"errors": [str(exc)]})
    finally:
        # D-18..D-19: object keys only — object counts do NOT change on update
        # (include_hierarchy=False). The objecttype/schema count keys stay
        # valid. The 60s public TTL bounds worst-case staleness; the purge is
        # best-effort / fail-open (D-21).
        await purge_public_object_cache(
            object_id_str, object_type_id_str, schema_id_str,
            include_hierarchy=False,
        )

    # 200 + fresh assembled shape (same as POST; the create path's D-23
    # cache SEED is intentionally NOT replicated here — a PUT consumer
    # GET naturally misses and re-assembles via load_object_read, and
    # seeding the new shape would race with the cache-purge pattern above).
    data = await load_object_read(db, object_id)
    shape = build_object_shape(data)
    return await record_filter_object_shape(
        db, shape, object_id, str(current_user.id), is_superuser=current_user.is_superuser
    )


# Plan 05: DELETE /public/api/v1/object/{id} — 204 + shared-pipeline delete
# (WRIT-03 + WRIT-04). D-16: no per-route rate-limit decorator — the global
# middleware enforces. The permission dep chain has already produced
# 401/403/404 (D-14 invisible DENY) by the time the handler body runs.
@object_write_router.delete("/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
async def public_delete_object(
    object_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_object_write("delete")),
):
    # Capture object_type_id + schema_id BEFORE deletion — they're needed
    # for the D-19 hierarchy-inclusive purge (object/objecttype/schema
    # count keys all change on a delete). The existence check doubles as
    # the legitimate 404 path for a nonexistent id; DENY → 404 inside the
    # permission dep returns the SAME detail string ("Object not found"),
    # so DENY and nonexistent are indistinguishable (D-14 — no existence
    # leak on the public surface).
    obj = (
        await db.execute(
            select(Object).where(Object.id == object_id)
        )
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="Object not found")

    # CR-03: the per-value DELETE route checks the attribute-type `delete`
    # record DENY before removing a row; the composite object delete removes
    # every value/reference at once, so it must run the same gate here. The
    # check is before `delete_object`, so a denied request writes nothing.
    await assert_object_delete_attribute_types_allowed(
        db, object_id,
        user_id=str(current_user.id),
        is_superuser=current_user.is_superuser,
    )

    # WRIT-04: shared-service delete — the entire pipeline (audit row
    # with the service user as actor, IndexOutbox 'remove' entry, ES
    # remove-job enqueue, internal cache:object:* invalidation) is fired
    # by the shared delete_object. The DB FK ondelete="CASCADE" on
    # ObjectValue.object_id / ObjectRef.from_object_id /
    # ObjectRef.to_object_id removes the dependent value + ref rows in
    # the same transaction. The dep chain (D-15) already verified
    # type-level objects:delete + record-level resolution; reaching
    # this point guarantees a real delete is authorized.
    actor_id = uuid.UUID(str(current_user.id))
    # CORESEAM-11/D-14: the public delete carries its own Core-owned user origin
    # (not the internal `object.delete` default), so a subscriber can tell a
    # public-API delete from an admin/internal one. Client payloads cannot
    # choose it (T-291.2-13).
    delete_origin = user_origin(PUBLIC_OBJECT_DELETE_ORIGIN)
    # 290 D-15: the shared delete_object raises ValueLockConflict for a type
    # that is still externally managed — mapped to the public 409 errors
    # contract here (the permission dep and the 404 check above ran first).
    try:
        deleted = await delete_object(
            db, object_id, actor_id=actor_id, origin=delete_origin
        )
    except ValueLockConflict as exc:
        raise HTTPException(status_code=409, detail={"errors": [str(exc)]})
    if not deleted:
        # Race-deleted between the existence check above and the
        # delete_object call (concurrent request). Same 404 detail as a
        # nonexistent id, so the consumer sees no information gain.
        raise HTTPException(status_code=404, detail="Object not found")

    # D-18..D-19: object keys + hierarchy keys (object/objecttype/schema
    # count keys all change on a delete — objectCount decreases by 1,
    # so the objecttype + schema count caches are stale). include_hierarchy
    # =True is the default; the helper is best-effort / fail-open (D-21).
    await purge_public_object_cache(
        str(object_id), str(obj.object_type_id), str(obj.schema_id),
        include_hierarchy=True,
    )
    return None  # 204 No Content — empty body, no shape (DEV-04)

