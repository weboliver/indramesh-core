import uuid
from collections import defaultdict
from typing import List

from fastapi import HTTPException
from sqlalchemy import select, func, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Schema, Source, ObjectType, Object, ObjectTypeAttribute, AttributeType, Icon
from app.models import Language, SchemaLanguage
from app.models.index_outbox import IndexOutbox, IndexOutboxStatus
from app.models.reference_type import ReferenceType
from app.models.attribute_type import AttributeTypeDataType
from app.models.status import Status, StatusCategory
from app.public_api.objecttype_schema_cache import purge_public_objecttype_schema_cache
from app.schemas.crud import (
    SchemaCreate,
    SchemaUpdate,
    SchemaResponse,
    AtomicCreateRequest,
    schema_name_to_slug,
)
from app.services.attribute_type_aliases import TYPE_STRING_MAP
from app.services.object_type_service import _provision_system_attributes
from app.services.keygen_service import (
    create_schema_sequence, drop_schema_sequence, sequence_name
)
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.services.record_permission_service import build_record_permission_filter, delete_dependent_record_permissions
from app.services.select_option_service import ensure_option_ids
from app.services.translation_service import delete_dependent_translations
from app.db.redis import get_arq_pool
from app.db import async_session_maker
from app.services.audit_service import audit_log, model_to_dict
from app.services.value_lock_service import assert_types_deletable
from app.services.classification_service import (
    UnknownClassificationLabel,
    load_classifications,
    load_label_key_map,
    resolve_template_classification,
    set_labels,
    template_classification,
)


class InvalidSchemaLanguage(ValueError):
    """A schema default-language change rejected by Core language validation.

    Domain error (D-01/D-03): the REST schema routes translate it back to the
    original HTTP 400 with the same detail, while the SDK adapter maps it to
    ``CoreDataInvalid`` (``invalid_input``) carrying the same safe message —
    ``HTTPException`` never crosses the SDK write boundary.
    """


async def _enqueue_schema_index_job(schema_id: uuid.UUID, job_name: str) -> None:
    try:
        arq_pool = await get_arq_pool()
        outbox_id = uuid.uuid4()
        async with async_session_maker() as db:
            db.add(IndexOutbox(
                id=outbox_id,
                object_id=schema_id,
                operation="index_schema" if "index" in job_name else "remove_schema",
                status=IndexOutboxStatus.ENQUEUED.value,
            ))
            await db.commit()
        job_id = f"{job_name}_{schema_id}_{outbox_id}"
        await arq_pool.enqueue_job(
            job_name,
            str(schema_id),
            outbox_id=str(outbox_id),
            _job_id=job_id,
        )
    except Exception:
        pass


async def enqueue_schema_search_removal(
    schema_id: uuid.UUID, object_type_ids: List[uuid.UUID]
) -> None:
    """Best-effort search cleanup for a schema whose rows are already gone."""
    # D-20: Remove each contained ObjectType's documents from the shared Schema
    # ES index.
    try:
        arq_pool = await get_arq_pool()
        for object_type_id in object_type_ids:
            await arq_pool.enqueue_job(
                "remove_object_type_job",
                str(object_type_id),
                str(schema_id),
                _job_id=f"remove_object_type_{object_type_id}_{uuid.uuid4().hex}",
            )
    except Exception:
        pass
    # Mark any pending index outbox entries as DONE so the sweeper
    # never re-enqueues them for a now-deleted schema
    try:
        async with async_session_maker() as outbox_db:
            await outbox_db.execute(
                update(IndexOutbox).where(
                    IndexOutbox.object_id == schema_id,
                    IndexOutbox.operation == "index_schema",
                    IndexOutbox.status.in_(["PENDING", "ENQUEUED"]),
                ).values(status=IndexOutboxStatus.DONE.value)
            )
            await outbox_db.commit()
    except Exception:
        pass
    # D-20: Remove from Elasticsearch (best-effort)
    await _enqueue_schema_index_job(schema_id, "remove_schema_job")


async def create_schema(
    db: AsyncSession,
    schema_data: SchemaCreate,
    actor_id: uuid.UUID,
    *,
    id: uuid.UUID | None = None,
) -> Schema | None:
    result = await db.execute(select(Source).where(Source.id == schema_data.source_id))
    source = result.scalar_one_or_none()
    if not source:
        return None
    db_schema = Schema(
        id=id or uuid.uuid4(),
        source_id=schema_data.source_id,
        key=schema_data.key,
        name=schema_data.name,
        description=schema_data.description,
        keygen=schema_data.keygen or schema_data.key or schema_name_to_slug(schema_data.name),
        primary_color=schema_data.primary_color,
        header_bg_color=schema_data.header_bg_color,
        sidebar_bg_color=schema_data.sidebar_bg_color,
        section_bg_color=schema_data.section_bg_color,
        surface_bg_color=schema_data.surface_bg_color,
        dark_primary_color=schema_data.dark_primary_color,
        dark_header_bg_color=schema_data.dark_header_bg_color,
        dark_sidebar_bg_color=schema_data.dark_sidebar_bg_color,
        dark_section_bg_color=schema_data.dark_section_bg_color,
        dark_surface_bg_color=schema_data.dark_surface_bg_color,
        status_filter_config=schema_data.status_filter_config,
    )
    db.add(db_schema)
    await db.flush()
    # 246 D-10: every schema gets a per-schema keygen SEQUENCE for the
    # `keygen-Nummer` counter. Idempotent (IF NOT EXISTS).
    await create_schema_sequence(db, db_schema.id)
    new_values = model_to_dict(db_schema)
    await audit_log(db, "schemas", db_schema.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_schema, ["languages"])
    await invalidate_entity_cache("schema")
    # D-20: Sync to Elasticsearch (best-effort, non-blocking)
    await _enqueue_schema_index_job(db_schema.id, "index_schema_job")
    return db_schema


async def list_schemas(
    db: AsyncSession, source_id: uuid.UUID | None = None, name: str | None = None, skip: int = 0, limit: int = 100,
    user_id: str | None = None, is_superuser: bool = False,
) -> tuple[List[Schema], int]:
    cache_key = build_list_cache_key(
        "schema", source_id=str(source_id) if source_id else "all", name=name or "", skip=skip, limit=limit,
        user_id=user_id,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    query = select(Schema).options(
        selectinload(Schema.default_language), selectinload(Schema.languages)
    ).order_by(Schema.created_at.asc(), Schema.id.asc())
    count_query = select(func.count()).select_from(Schema)
    if source_id is not None:
        query = query.where(Schema.source_id == source_id)
        count_query = count_query.where(Schema.source_id == source_id)
    if name:
        query = query.where(Schema.name.ilike(f"%{name}%"))
        count_query = count_query.where(Schema.name.ilike(f"%{name}%"))
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db, user_id, "schema", is_superuser=is_superuser
        )
        if filter_clause is not None:
            query = query.where(filter_clause)
            count_query = count_query.where(filter_clause)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": [SchemaResponse.model_validate(item).model_dump() for item in items], "total": total},
        tags=[build_tag_key("schema")],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_schema(db: AsyncSession, schema_id: uuid.UUID) -> Schema | dict | None:
    cache_key = build_cache_key("schema", str(schema_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Schema)
        .options(selectinload(Schema.default_language), selectinload(Schema.languages))
        .where(Schema.id == schema_id)
    )
    schema = result.scalar_one_or_none()
    if schema:
        await set_cached_with_tags(
            cache_key,
            SchemaResponse.model_validate(schema).model_dump(),
            tags=[build_tag_key("schema")],
            ttl=CACHE_TTL_ENTITY,
        )
    return schema


async def update_schema(
    db: AsyncSession, schema_id: uuid.UUID, schema_update: SchemaUpdate, actor_id: uuid.UUID
) -> Schema | None:
    result = await db.execute(select(Schema).where(Schema.id == schema_id))
    schema = result.scalar_one_or_none()
    if not schema:
        return None
    old_values = model_to_dict(schema)
    if schema_update.source_id is not None:
        result = await db.execute(
            select(Source).where(Source.id == schema_update.source_id)
        )
        source = result.scalar_one_or_none()
        if not source:
            return None
        if schema.source_id != schema_update.source_id:
            schema.source_id = schema_update.source_id
    if schema_update.name is not None and schema.name != schema_update.name:
        schema.name = schema_update.name
    if schema_update.description is not None and schema.description != schema_update.description:
        schema.description = schema_update.description
    if schema_update.key is not None and schema.key != schema_update.key:
        schema.key = schema_update.key
    # 246 D-12 / 290 D1 + DECOUPLE-04: keygen rename regenerates keys for
    # objects that actually have a native key (objects.key IS NOT NULL).
    # Keyless rows (CSV-imported, legacy) stay untouched — no retroactive key
    # creation. The counter is a fresh nextval per regenerated object.
    keygen_changed = (
        schema_update.keygen is not None
        and schema_update.keygen != schema.keygen
    )
    if keygen_changed:
        new_keygen = schema_update.keygen
        obj_result = await db.execute(
            select(Object.id).where(
                Object.schema_id == schema_id,
                Object.key.isnot(None),
            )
        )
        native_ids = [row[0] for row in obj_result.all()]
        if native_ids:
            for obj_id in native_ids:
                seq_result = await db.execute(text(
                    f"SELECT nextval('{sequence_name(schema_id)}')"
                ))
                next_num = seq_result.scalar_one()
                new_key = f"{new_keygen}-{next_num}"
                await db.execute(
                    update(Object).where(Object.id == obj_id).values(key=new_key)
                )
                # 253 D-05c: objects.key is mirrored into a real ObjectValue row
                # since Phase 253 (D-01), and a stored row wins over the
                # synthesized one on the read path — so the mirror has to move
                # with the column or the object would display its old key
                # forever. Joining through object_values.object_type_attribute_id
                # (rather than the ObjectType) also covers Weg-A children whose
                # Key OTA lives in the defining parent. Same transaction as the
                # column update: a failure must not persist half a rename.
                await db.execute(
                    text(
                        "UPDATE object_values ov SET value_simple = :new_key "
                        "FROM object_type_attributes ota "
                        "WHERE ov.object_type_attribute_id = ota.id "
                        "AND ota.name = 'Key' AND ota.is_system = TRUE "
                        "AND ov.object_id = :obj_id"
                    ),
                    {"new_key": new_key, "obj_id": obj_id},
                )
        schema.keygen = new_keygen
        await audit_log(db, "schemas", schema_id, "UPDATE", actor_id,
                        old_values={"keygen": old_values.get("keygen")},
                        new_values={"keygen": new_keygen})
        # D-12: regenerated keys must not serve stale cached object payloads.
        await invalidate_entity_cache("object")
        # 253 D-05c: the mirrored Key rows moved too — without this the
        # object-value cache keeps serving the pre-rename keys.
        await invalidate_entity_cache("object_value")
    if schema_update.primary_color is not None and schema.primary_color != schema_update.primary_color:
        schema.primary_color = schema_update.primary_color
    if schema_update.header_bg_color is not None and schema.header_bg_color != schema_update.header_bg_color:
        schema.header_bg_color = schema_update.header_bg_color
    if schema_update.sidebar_bg_color is not None and schema.sidebar_bg_color != schema_update.sidebar_bg_color:
        schema.sidebar_bg_color = schema_update.sidebar_bg_color
    if schema_update.section_bg_color is not None and schema.section_bg_color != schema_update.section_bg_color:
        schema.section_bg_color = schema_update.section_bg_color
    if schema_update.surface_bg_color is not None and schema.surface_bg_color != schema_update.surface_bg_color:
        schema.surface_bg_color = schema_update.surface_bg_color
    if schema_update.dark_primary_color is not None and schema.dark_primary_color != schema_update.dark_primary_color:
        schema.dark_primary_color = schema_update.dark_primary_color
    if schema_update.dark_header_bg_color is not None and schema.dark_header_bg_color != schema_update.dark_header_bg_color:
        schema.dark_header_bg_color = schema_update.dark_header_bg_color
    if schema_update.dark_sidebar_bg_color is not None and schema.dark_sidebar_bg_color != schema_update.dark_sidebar_bg_color:
        schema.dark_sidebar_bg_color = schema_update.dark_sidebar_bg_color
    if schema_update.dark_section_bg_color is not None and schema.dark_section_bg_color != schema_update.dark_section_bg_color:
        schema.dark_section_bg_color = schema_update.dark_section_bg_color
    if schema_update.dark_surface_bg_color is not None and schema.dark_surface_bg_color != schema_update.dark_surface_bg_color:
        schema.dark_surface_bg_color = schema_update.dark_surface_bg_color
    if schema_update.status_filter_config is not None and schema.status_filter_config != schema_update.status_filter_config:
        schema.status_filter_config = schema_update.status_filter_config
    if "default_language_id" in schema_update.model_fields_set:
        new_default_id = schema_update.default_language_id
        if new_default_id is not None:
            lang_result = await db.execute(select(Language).where(Language.id == new_default_id))
            lang = lang_result.scalar_one_or_none()
            if not lang:
                raise InvalidSchemaLanguage("Language not found")
            if not lang.is_active:
                raise InvalidSchemaLanguage(
                    f"Language '{lang.code}' is inactive. Only active languages can be a schema default."
                )
            subset_result = await db.execute(
                select(SchemaLanguage.language_id).where(SchemaLanguage.schema_id == schema_id)
            )
            enabled_ids = {row[0] for row in subset_result.all()}
            if new_default_id not in enabled_ids:
                raise InvalidSchemaLanguage(
                    f"Language '{lang.code}' is not enabled for this schema. Add it to the schema's languages first."
                )
        # explicit null (or validated id) — assign; D-07 escape hatch
        schema.default_language_id = new_default_id
    new_values = model_to_dict(schema)
    await audit_log(db, "schemas", schema_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    # Eager-load languages (multilingual @property) and default_language when set —
    # SchemaResponse serialization accesses both, and an unloaded relationship
    # would raise MissingGreenlet outside the greenlet context (same pattern as
    # search_service.py's refresh(obj, ['values'])).
    await db.refresh(
        schema, ["languages"] + (["default_language"] if schema.default_language_id is not None else [])
    )
    await invalidate_entity_cache("schema")
    # D-20: Sync to Elasticsearch
    await _enqueue_schema_index_job(schema_id, "index_schema_job")
    return schema


async def invalidate_schema_tree_caches(object_type_ids: List[uuid.UUID]) -> None:
    """Drop every cached read of what a schema or source delete removed."""
    for entity_type in (
        "schema",
        "object_type",
        "object_type_attribute",
        "object",
        "object_value",
        "object_ref",
        "status",
        "reference_type",
        "search",
    ):
        await invalidate_entity_cache(entity_type)
    await purge_public_objecttype_schema_cache([str(type_id) for type_id in object_type_ids])


async def delete_schema(db: AsyncSession, schema_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    result = await db.execute(select(Schema).where(Schema.id == schema_id))
    schema = result.scalar_one_or_none()
    if not schema:
        return False
    # 290 D-15: a locked member ObjectType protects the whole schema — the
    # fresh persisted markers (FOR SHARE, never the cache) are checked before
    # any audit/purge/delete so a refused delete has zero side effects.
    ot_result = await db.execute(
        select(ObjectType).where(ObjectType.schema_id == schema_id)
    )
    object_types = ot_result.scalars().all()
    object_type_ids = [ot.id for ot in object_types]
    await assert_types_deletable(db, object_type_ids)
    old_values = model_to_dict(schema)
    await audit_log(db, "schemas", schema_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await delete_dependent_translations(db, schema_ids=[schema_id])
    await delete_dependent_record_permissions(db, schema_ids=[schema_id])
    # D-XX: delete object types in leaf-first order (children before parents) to
    # avoid the CircularDependencyError SQLAlchemy raises when a self-referential
    # parent_id (ondelete=CASCADE) makes the delete graph unorderable. The
    # schema-level cascade handles the remaining rows once the cycle is broken.
    if object_types:
        # ObjectTypeAttribute rows first — including the label_attribute_id
        # self-FK (SET NULL) which otherwise cycles against its ObjectType.
        ota_result = await db.execute(
            select(ObjectTypeAttribute).where(
                ObjectTypeAttribute.object_type_id.in_([ot.id for ot in object_types])
            )
        )
        attributes = ota_result.scalars().all()
        for ota in attributes:
            await db.delete(ota)
        by_id = {ot.id: ot for ot in object_types}
        # depth = number of ancestors; deeper types deleted first
        def depth(ot):
            d = 0
            current = ot
            seen = set()
            while current.parent_id and current.parent_id in by_id and current.parent_id not in seen:
                seen.add(current.parent_id)
                current = by_id[current.parent_id]
                d += 1
            return d
        for ot in sorted(object_types, key=depth, reverse=True):
            await db.delete(ot)
    # 246 D-10 lifecycle: drop the per-schema keygen SEQUENCE with the schema.
    await drop_schema_sequence(db, schema_id)
    await db.delete(schema)
    await db.commit()
    await enqueue_schema_search_removal(schema_id, object_type_ids)
    # 290 D-15: the cascade removes every contained row, so no tier may keep serving them.
    await invalidate_schema_tree_caches(object_type_ids)
    return True


async def atomic_create_schema(db: AsyncSession, request: AtomicCreateRequest, actor_id: uuid.UUID | None = None) -> Schema:
    result = await db.execute(select(Source).where(Source.id == request.source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    at_result = await db.execute(select(AttributeType))
    attr_type_dict = {row.data_type: row.id for row in at_result.scalars().all()}

    # Template classification (D-08/D-18): resolve EVERY block against the
    # catalogue before the first write. An unknown label key therefore fails
    # with nothing created — not even the schema row and its keygen sequence.
    resolved_classifications: dict[tuple[int, int], dict] = {}
    if any(
        attr.classification is not None
        for ot in request.object_types
        for attr in ot.attributes
    ):
        label_key_map = await load_label_key_map(db)
        for ot_index, ot in enumerate(request.object_types):
            for attr_index, attr in enumerate(ot.attributes):
                if attr.classification is None:
                    continue
                resolved_classifications[(ot_index, attr_index)] = (
                    resolve_template_classification(attr.classification, label_key_map)
                )

    schema_create = SchemaCreate(
        source_id=request.source_id,
        name=request.name,
        description=request.description,
        key=schema_name_to_slug(request.name),
    )
    db_schema = Schema(**schema_create.model_dump())
    # 246 D-10: atomic (template/AI-wizard) schemas also get a keygen prefix
    # and a per-schema SEQUENCE, so native objects created after instantiation
    # get `keygen-Nummer` keys.
    db_schema.keygen = schema_create.keygen or schema_name_to_slug(request.name)
    db.add(db_schema)
    await db.flush()
    await create_schema_sequence(db, db_schema.id)

    # icon_type/icon_url (None for the original AI-wizard lucide-only flow)
    # carry a REAL icon's own type+URL through a schema template — cache key
    # must include them, not just icon_key, so two object types sharing a
    # lucide name but pointing at different real icons don't collide.
    icon_cache: dict[tuple[str, str], uuid.UUID] = {}
    for ot in request.object_types:
        ikey = ot.icon_key or "box"
        icon_type = ot.icon_type or "lucide"
        icon_url = ot.icon_url or f"lucide:{ikey}"
        cache_key = (icon_type, icon_url)
        if cache_key not in icon_cache:
            icon = Icon(schema_id=db_schema.id, name=ikey, local_name=ikey, url=icon_url, type=icon_type)
            db.add(icon)
            await db.flush()
            icon_cache[cache_key] = icon.id

    status_cache: dict[str, uuid.UUID] = {}
    status_meta_by_name = {s.name: s for s in request.statuses}

    ot_name_map = {}
    attr_ref_list = []
    ot_parent_list = []
    ref_type_cache: dict[str, uuid.UUID] = {}

    for sort_idx, ot in enumerate(request.object_types):
        ikey = ot.icon_key or "box"
        icon_type = ot.icon_type or "lucide"
        icon_url = ot.icon_url or f"lucide:{ikey}"
        db_ot = ObjectType(
            schema_id=db_schema.id,
            name=ot.name,
            description=ot.description,
            sort=sort_idx,
            icon_id=icon_cache.get((icon_type, icon_url)),
            inherits_attributes=ot.inherits_attributes,
            is_abstract=ot.is_abstract,
        )
        db.add(db_ot)
        await db.flush()
        ot_name_map[ot.name] = db_ot.id

        # 246 D-01: every ObjectType created via the AI-wizard/template path
        # gets the four system attributes (Name, Key, Created, Updated). The
        # helper's collision guard skips any the wizard itself declared.
        await _provision_system_attributes(db, db_ot.id, actor_id)

        if ot.parent_name:
            ot_parent_list.append((db_ot, ot.parent_name))

        for attr_idx, attr in enumerate(ot.attributes):
            enum_val = TYPE_STRING_MAP.get(attr.type_string.lower().strip())
            if enum_val is None:
                enum_val = AttributeTypeDataType.TEXT
            attribute_type_id = attr_type_dict.get(enum_val)

            reference_type_id = None
            if enum_val == AttributeTypeDataType.REFERENCE:
                rt_name = (attr.reference_type_name or attr.name).strip()
                if rt_name not in ref_type_cache:
                    existing_rt = await db.execute(
                        select(ReferenceType).where(
                            ReferenceType.reference == rt_name,
                            ReferenceType.schema_id == db_schema.id,
                        )
                    )
                    rt = existing_rt.scalar_one_or_none()
                    if not rt:
                        rt = ReferenceType(
                            reference=rt_name,
                            schema_id=db_schema.id,
                            source_id=db_schema.source_id,
                        )
                        db.add(rt)
                        await db.flush()
                    ref_type_cache[rt_name] = rt.id
                reference_type_id = ref_type_cache[rt_name]

            structure = None
            if attr.select_options:
                # D-02: normalize the raw List[str] into the canonical
                # {"id", "value", "position"} shape — no write site may store
                # an id-less options list (223 D-01/D-16).
                structure = {"options": ensure_option_ids(attr.select_options)}
            elif enum_val == AttributeTypeDataType.STATUS and attr.status_option_names:
                status_options = []
                for status_name in attr.status_option_names:
                    meta = status_meta_by_name.get(status_name)
                    if status_name not in status_cache:
                        category = None
                        if meta and meta.status_category:
                            try:
                                category = StatusCategory(meta.status_category)
                            except ValueError:
                                category = None
                        db_status = Status(
                            schema_id=db_schema.id,
                            name=status_name,
                            color=meta.color if meta else None,
                            status_category=category,
                        )
                        db.add(db_status)
                        await db.flush()
                        status_cache[status_name] = db_status.id
                    status_options.append({
                        "id": str(status_cache[status_name]),
                        "name": status_name,
                        "color": meta.color if meta else None,
                        "status_category": meta.status_category if meta else None,
                    })
                if status_options:
                    structure = {"status_options": status_options}

            db_attr = ObjectTypeAttribute(
                object_type_id=db_ot.id,
                name=attr.name,
                description=attr.description,
                sort=attr_idx,
                attribute_type_id=attribute_type_id,
                is_required=attr.required,
                is_unique=attr.is_unique,
                default_value=attr.default_value,
                structure=structure,
                referenced_object_type_id=None,
                reference_type_id=reference_type_id,
            )
            db.add(db_attr)
            await db.flush()

            resolved = resolved_classifications.get((sort_idx, attr_idx))
            if resolved is not None:
                await set_labels(db, "attribute", db_attr.id, **resolved)

            if attr.referenced_object_type_name:
                attr_ref_list.append((db_attr, attr.referenced_object_type_name))

    for db_attr, ref_name in attr_ref_list:
        target_id = ot_name_map.get(ref_name)
        if target_id:
            db_attr.referenced_object_type_id = target_id

    # Resolve ObjectType hierarchy (parent_name -> parent_id) in a second pass,
    # same reason as the reference resolution above — a child's parent may be
    # defined later in request.object_types, so ot_name_map is only complete
    # once every ObjectType row has been created. Without this, instantiating
    # a schema template always produced a flat structure regardless of the
    # source schema's hierarchy ("Anlegen funktioniert jetzt aber nur ohne
    # Hierarchie", confirmed 2026-07-27 — get_schema_template_data() never
    # captured ObjectType.parent_id at all).
    for db_ot, parent_name in ot_parent_list:
        parent_id = ot_name_map.get(parent_name)
        if parent_id:
            db_ot.parent_id = parent_id

    new_values = model_to_dict(db_schema)
    await audit_log(db, "schemas", db_schema.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_schema, ["languages"])
    await invalidate_entity_cache("schema")
    return db_schema


# --- Strict schema-from-tree materializer (SEAM-04, D-08/D-09/D-10) -----------
#
# The ONE authoritative schema creation path for the plugin wizard and the Core
# wizard. It validates the ENTIRE tree before writing anything, rejects an
# invalid tree in full with a concrete indexed path (D-09) and creates exactly
# one new schema in an existing Source — a name/key conflict is reported and no
# adoption, merge or rename occurs (D-10). The existing manual/template create
# paths keep their behaviour: only the strict path is hardened here.


class InvalidSchemaTree(ValueError):
    """A whole-tree preflight rejection with a concrete indexed tree path (D-09).

    ``path`` is the concrete tree location (e.g. ``object_types[1].parent_name``)
    and ``reason`` a bounded, Core-authored explanation. The Core route maps it
    to a 422 with ``{"path", "reason"}``.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class SchemaNameConflict(ValueError):
    """A source-local schema name/key conflict (D-10) — never adopt/merge/rename."""


class SchemaTreeSourceNotFound(ValueError):
    """The target Source for a schema-from-tree create does not exist."""


def _iter_parent_chain_error(object_types) -> str | None:
    """Return the error path of a parent cycle, or ``None`` when acyclic."""
    parent_of: dict[str, str] = {}
    for ot in object_types:
        if ot.parent_name:
            parent_of[ot.name.strip().lower()] = ot.parent_name.strip().lower()
    for index, ot in enumerate(object_types):
        if not ot.parent_name:
            continue
        seen: set[str] = set()
        current = ot.name.strip().lower()
        while current in parent_of:
            if current in seen:
                return f"object_types[{index}].parent_name"
            seen.add(current)
            current = parent_of[current]
    return None


def _preflight_schema_tree(request) -> None:
    """Validate the whole tree before any write (D-09).

    ``request`` is any object exposing ``object_types`` and ``statuses`` — the
    strict :class:`SchemaTreeCreate` SDK/HTTP DTO on the final-create path and,
    for the read-only plugin seam, the neutral :class:`SchemaTreeDraft`
    (``core_data.validate_schema_tree_draft``). Both shapes share this ONE
    semantic authority, so a draft validated by the plugin is accepted
    unchanged by final creation.

    Raises :class:`InvalidSchemaTree` with a concrete indexed path for an
    unknown attribute type, a duplicate object-type/attribute/status name, a
    missing parent, a self-parent, a parent cycle or a missing reference target.
    """
    object_types = list(request.object_types)

    # ObjectType names — duplicate detection on the exact normalized name.
    seen_ot: dict[str, int] = {}
    for index, ot in enumerate(object_types):
        key = ot.name.strip().lower()
        if key in seen_ot:
            raise InvalidSchemaTree(
                f"object_types[{index}].name",
                f"duplicate object type name {ot.name!r}",
            )
        seen_ot[key] = index

    # Schema-level statuses — duplicate detection.
    seen_status: dict[str, int] = {}
    for index, status in enumerate(request.statuses):
        key = status.name.strip().lower()
        if key in seen_status:
            raise InvalidSchemaTree(
                f"statuses[{index}].name",
                f"duplicate status name {status.name!r}",
            )
        seen_status[key] = index

    # Attributes: duplicate names, known alias normalization, reference targets.
    for ot_index, ot in enumerate(object_types):
        seen_attr: dict[str, int] = {}
        for attr_index, attr in enumerate(ot.attributes):
            attr_path = f"object_types[{ot_index}].attributes[{attr_index}]"
            key = attr.name.strip().lower()
            if key in seen_attr:
                raise InvalidSchemaTree(
                    f"{attr_path}.name",
                    f"duplicate attribute name {attr.name!r}",
                )
            seen_attr[key] = attr_index
            enum_val = TYPE_STRING_MAP.get((attr.type_string or "").lower().strip())
            if enum_val is None:
                raise InvalidSchemaTree(
                    f"{attr_path}.type_string",
                    f"unknown attribute type {attr.type_string!r}",
                )
            if enum_val == AttributeTypeDataType.REFERENCE:
                target = (attr.referenced_object_type_name or "").strip().lower()
                if not target:
                    raise InvalidSchemaTree(
                        f"{attr_path}.referenced_object_type_name",
                        "a reference attribute requires a target object type name",
                    )
                if target not in seen_ot:
                    raise InvalidSchemaTree(
                        f"{attr_path}.referenced_object_type_name",
                        f"reference target {attr.referenced_object_type_name!r} is not part of this tree",
                    )
            elif (attr.referenced_object_type_name or "").strip():
                # D-09: a stray reference target on a non-reference type is
                # invalid — reject it in full rather than silently writing a
                # reference binding (or crashing the materializer) later.
                raise InvalidSchemaTree(
                    f"{attr_path}.referenced_object_type_name",
                    f"a {attr.type_string!r} attribute must not declare a reference target",
                )

            # Type-specific default/structure validation (D-09).
            if attr.default_value not in (None, ""):
                raw_default = attr.default_value.strip()
                if enum_val == AttributeTypeDataType.INTEGER:
                    try:
                        int(raw_default)
                    except ValueError:
                        raise InvalidSchemaTree(
                            f"{attr_path}.default_value",
                            "default value must be an integer",
                        ) from None
                elif enum_val == AttributeTypeDataType.DOUBLE:
                    try:
                        float(raw_default)
                    except ValueError:
                        raise InvalidSchemaTree(
                            f"{attr_path}.default_value",
                            "default value must be a number",
                        ) from None
                elif enum_val == AttributeTypeDataType.BOOLEAN:
                    if raw_default.lower() not in ("true", "false"):
                        raise InvalidSchemaTree(
                            f"{attr_path}.default_value",
                            "default value must be 'true' or 'false'",
                        )
            if (
                enum_val == AttributeTypeDataType.SELECT
                and attr.select_options is not None
                and not attr.select_options
            ):
                raise InvalidSchemaTree(
                    f"{attr_path}.select_options",
                    "a select attribute with options must declare at least one option",
                )

    # Parents: existence, self-parent and cycles.
    for index, ot in enumerate(object_types):
        if not ot.parent_name:
            continue
        parent_key = ot.parent_name.strip().lower()
        if parent_key == ot.name.strip().lower():
            raise InvalidSchemaTree(
                f"object_types[{index}].parent_name",
                "an object type cannot be its own parent",
            )
        if parent_key not in seen_ot:
            raise InvalidSchemaTree(
                f"object_types[{index}].parent_name",
                f"parent {ot.parent_name!r} is not part of this tree",
            )
    cycle_path = _iter_parent_chain_error(object_types)
    if cycle_path is not None:
        raise InvalidSchemaTree(cycle_path, "parent cycle detected")


async def create_schema_from_tree(
    db: AsyncSession, request, actor_id: uuid.UUID | None = None
) -> Schema:
    """Strictly create one schema (and its whole tree) in an existing Source.

    ``request`` is the strict :class:`SchemaTreeCreate` SDK DTO. The whole tree
    is validated before any write; object types are created first, the hierarchy
    is resolved, then system attributes are provisioned with the hierarchy
    present, then user attributes/reference/status links, then the keygen
    sequence and the CREATE audit — all committed exactly once. Any failure
    leaves no schema, object type, attribute, sequence or audit row behind.
    """
    result = await db.execute(select(Source).where(Source.id == request.source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise SchemaTreeSourceNotFound("Source not found")

    # Serialize strict same-source creates only (strict vs strict). The manual
    # writers are intentionally untouched (R05-3); the residual manual-vs-strict
    # race is accepted because the manual paths keep their behaviour.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:sid))"),
        {"sid": str(request.source_id)},
    )

    _preflight_schema_tree(request)

    # Template classification (D-08/D-18): resolve EVERY block against the
    # catalogue before any write. An unknown label key refuses the whole tree
    # through the same path-based contract every other tree problem uses, so
    # nothing is created (no schema, object type, attribute or sequence).
    resolved_classifications: dict[tuple[int, int], dict] = {}
    if any(
        getattr(attr, "classification", None) is not None
        for ot in request.object_types
        for attr in ot.attributes
    ):
        label_key_map = await load_label_key_map(db)
        for ot_index, ot in enumerate(request.object_types):
            for attr_index, attr in enumerate(ot.attributes):
                block = getattr(attr, "classification", None)
                if block is None:
                    continue
                try:
                    resolved_classifications[(ot_index, attr_index)] = (
                        resolve_template_classification(block, label_key_map)
                    )
                except UnknownClassificationLabel as exc:
                    raise InvalidSchemaTree(
                        f"object_types[{ot_index}].attributes[{attr_index}].classification",
                        f"unknown classification label '{exc.family}/{exc.key}'",
                    ) from None

    slug = schema_name_to_slug(request.name)
    existing = await db.execute(
        select(Schema).where(Schema.source_id == request.source_id)
    )
    for row in existing.scalars().all():
        if (
            (row.name or "").strip().lower() == request.name.strip().lower()
            or (row.keygen or "") == slug
            or (row.key or "") == slug
        ):
            raise SchemaNameConflict(
                f"A schema named {request.name!r} already exists in this Source"
            )

    at_result = await db.execute(select(AttributeType))
    attr_type_dict = {row.data_type: row.id for row in at_result.scalars().all()}

    db_schema = Schema(
        source_id=request.source_id,
        key=slug,
        name=request.name,
        description=request.description,
        keygen=slug,
    )
    db.add(db_schema)
    await db.flush()
    # 246 D-10: the per-schema keygen SEQUENCE is created inside this one
    # transaction, so a later failure rolls it back with the owned rows.
    await create_schema_sequence(db, db_schema.id)

    icon_cache: dict[tuple[str, str], uuid.UUID] = {}
    for ot in request.object_types:
        ikey = ot.icon_key or "box"
        icon_type = ot.icon_type or "lucide"
        icon_url = ot.icon_url or f"lucide:{ikey}"
        cache_key = (icon_type, icon_url)
        if cache_key not in icon_cache:
            icon = Icon(
                schema_id=db_schema.id,
                name=ikey,
                local_name=ikey,
                url=icon_url,
                type=icon_type,
            )
            db.add(icon)
            await db.flush()
            icon_cache[cache_key] = icon.id

    name_to_ot: dict[str, ObjectType] = {}
    for sort_idx, ot in enumerate(request.object_types):
        ikey = ot.icon_key or "box"
        icon_type = ot.icon_type or "lucide"
        icon_url = ot.icon_url or f"lucide:{ikey}"
        db_ot = ObjectType(
            schema_id=db_schema.id,
            name=ot.name,
            description=ot.description,
            sort=sort_idx,
            icon_id=icon_cache.get((icon_type, icon_url)),
            inherits_attributes=ot.inherits_attributes,
            is_abstract=ot.is_abstract,
        )
        db.add(db_ot)
        await db.flush()
        name_to_ot[ot.name.strip().lower()] = db_ot

    # Hierarchy is provisioned BEFORE system attributes: the provisioning helper
    # consults inheriting ancestors, which only exist once parent_id is set.
    for ot in request.object_types:
        if ot.parent_name:
            name_to_ot[ot.name.strip().lower()].parent_id = (
                name_to_ot[ot.parent_name.strip().lower()].id
            )
    await db.flush()

    for ot in request.object_types:
        await _provision_system_attributes(
            db, name_to_ot[ot.name.strip().lower()].id, actor_id
        )

    status_cache: dict[str, uuid.UUID] = {}
    status_meta_by_name = {s.name.strip().lower(): s for s in request.statuses}
    attr_ref_list: list[tuple[ObjectTypeAttribute, str]] = []

    for ot_index, ot in enumerate(request.object_types):
        db_ot = name_to_ot[ot.name.strip().lower()]
        for attr_idx, attr in enumerate(ot.attributes):
            enum_val = TYPE_STRING_MAP.get(
                (attr.type_string or "").lower().strip()
            )
            attribute_type_id = attr_type_dict.get(enum_val)

            reference_type_id = None
            if enum_val == AttributeTypeDataType.REFERENCE:
                rt_name = (attr.reference_type_name or attr.name).strip()
                existing_rt = await db.execute(
                    select(ReferenceType).where(
                        ReferenceType.reference == rt_name,
                        ReferenceType.schema_id == db_schema.id,
                    )
                )
                rt = existing_rt.scalar_one_or_none()
                if not rt:
                    rt = ReferenceType(
                        reference=rt_name,
                        schema_id=db_schema.id,
                        source_id=db_schema.source_id,
                    )
                    db.add(rt)
                    await db.flush()
                reference_type_id = rt.id

            structure = None
            if attr.select_options:
                structure = {"options": ensure_option_ids(attr.select_options)}
            elif (
                enum_val == AttributeTypeDataType.STATUS
                and attr.status_option_names
            ):
                status_options = []
                for status_name in attr.status_option_names:
                    meta = status_meta_by_name.get(status_name.strip().lower())
                    if status_name.strip().lower() not in status_cache:
                        category = None
                        if meta and meta.status_category:
                            try:
                                category = StatusCategory(meta.status_category)
                            except ValueError:
                                category = None
                        db_status = Status(
                            schema_id=db_schema.id,
                            name=status_name,
                            color=meta.color if meta else None,
                            status_category=category,
                        )
                        db.add(db_status)
                        await db.flush()
                        status_cache[status_name.strip().lower()] = db_status.id
                    status_options.append({
                        "id": str(status_cache[status_name.strip().lower()]),
                        "name": status_name,
                        "color": meta.color if meta else None,
                        "status_category": meta.status_category if meta else None,
                    })
                if status_options:
                    structure = {"status_options": status_options}

            db_attr = ObjectTypeAttribute(
                object_type_id=db_ot.id,
                name=attr.name,
                description=attr.description,
                sort=attr_idx,
                attribute_type_id=attribute_type_id,
                is_required=attr.required,
                is_unique=attr.is_unique,
                default_value=attr.default_value,
                structure=structure,
                referenced_object_type_id=None,
                reference_type_id=reference_type_id,
            )
            db.add(db_attr)
            await db.flush()

            resolved = resolved_classifications.get((ot_index, attr_idx))
            if resolved is not None:
                await set_labels(db, "attribute", db_attr.id, **resolved)

            if (
                enum_val == AttributeTypeDataType.REFERENCE
                and attr.referenced_object_type_name
            ):
                attr_ref_list.append((db_attr, attr.referenced_object_type_name))

    for db_attr, ref_name in attr_ref_list:
        target = name_to_ot.get(ref_name.strip().lower())
        if target is None:
            # Defensive: _preflight_schema_tree rejects a missing target before
            # any write, so this is unreachable through the public paths.
            raise InvalidSchemaTree(
                "object_types.attributes.referenced_object_type_name",
                f"reference target {ref_name!r} is not part of this tree",
            )
        db_attr.referenced_object_type_id = target.id

    new_values = model_to_dict(db_schema)
    await audit_log(
        db, "schemas", db_schema.id, "CREATE", actor_id,
        old_values=None, new_values=new_values,
    )
    await db.commit()
    await db.refresh(db_schema, ["languages"])
    await invalidate_entity_cache("schema")
    return db_schema


async def get_schema_template_data(db: AsyncSession, schema_id: uuid.UUID) -> dict:
    result = await db.execute(select(Schema).where(Schema.id == schema_id))
    schema = result.scalar_one_or_none()
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")

    ot_result = await db.execute(
        select(ObjectType).where(ObjectType.schema_id == schema_id).order_by(ObjectType.sort)
    )
    object_types = ot_result.scalars().all()

    at_result = await db.execute(select(AttributeType))
    attr_type_map = {row.id: getattr(row.data_type, "value", row.data_type) for row in at_result.scalars().all()}

    icon_result = await db.execute(select(Icon).where(Icon.schema_id == schema_id))
    # Capture the icon's real type/url, not just a name string — schemas can
    # carry real (non-lucide) icons (type="url", a real image URL); collapsing
    # that down to a bare name and always recreating as type="lucide" on
    # instantiate silently lost the icon entirely
    # ("Icons werden nicht mit abgespeichert", confirmed 2026-07-27).
    icon_map = {
        icon.id: {
            "name": icon.name or icon.local_name or "box",
            "local_name": icon.local_name,
            "url": icon.url,
            "type": icon.type,
        }
        for icon in icon_result.scalars().all()
    }

    ot_ids = [ot.id for ot in object_types]

    all_attrs_result = await db.execute(
        select(ObjectTypeAttribute)
        .where(ObjectTypeAttribute.object_type_id.in_(ot_ids))
        .order_by(ObjectTypeAttribute.object_type_id, ObjectTypeAttribute.sort)
    )
    all_attrs = all_attrs_result.scalars().all()

    attrs_by_ot = defaultdict(list)
    for attr in all_attrs:
        attrs_by_ot[attr.object_type_id].append(attr)

    # One batch read of every attribute's labels: the template carries stable
    # catalogue keys per attribute (D-08), never instance label ids, so the
    # payload stays valid in another instance.
    attribute_classifications = await load_classifications(
        db, "attribute", [attr.id for attr in all_attrs]
    )

    ref_ids = {attr.referenced_object_type_id for attr in all_attrs if attr.referenced_object_type_id}
    ref_name_map = {}
    if ref_ids:
        ref_result = await db.execute(
            select(ObjectType.id, ObjectType.name).where(ObjectType.id.in_(ref_ids))
        )
        ref_name_map = {row.id: row.name for row in ref_result}

    ot_id_to_name = {ot.id: ot.name for ot in object_types}
    status_meta_by_name: dict[str, dict] = {}

    ot_list = []
    for ot in object_types:
        attrs = attrs_by_ot.get(ot.id, [])

        attr_list = []
        for attr in attrs:
            type_string = getattr(attr_type_map.get(attr.attribute_type_id), "value",
                                  attr_type_map.get(attr.attribute_type_id)) or "text"
            ref_name = ref_name_map.get(attr.referenced_object_type_id)

            # ObjectTypeAttribute.structure["options"] has two shapes depending on
            # how the attribute was created: atomic_create_schema() stores a plain
            # List[str], while other writers historically stored
            # List[{"value": ..., "position": ...}].
            # select_options is typed List[str], so passing the dict shape straight
            # through made instantiate_schema_template() 500 with an unhandled
            # Pydantic ValidationError for any Select attribute carrying that
            # historical shape (confirmed live, 2026-07-27). Normalize both shapes here,
            # same tolerant pattern AttributeMiniEditor.jsx already uses on the
            # frontend for the same dual-shape reason.
            select_options = None
            if attr.structure and isinstance(attr.structure, dict):
                raw_options = attr.structure.get("options")
                if raw_options:
                    select_options = [
                        (opt if isinstance(opt, str) else opt.get("value") or opt.get("label"))
                        for opt in raw_options
                    ]
                    select_options = [o for o in select_options if o]

            # Status is a schema-level entity (Status model), referenced by a
            # STATUS-typed attribute's structure["status_options"] (a filtered
            # subset of the schema's own Status rows, by id). type_string alone
            # used to round-trip to AttributeTypeDataType.TEXT on instantiate
            # (TYPE_STRING_MAP had no "status" entry — "Status Attribute wird
            # beim Template zu einem Text Objekt", confirmed 2026-07-27).
            # Collect the distinct Status definitions by name (deduped across
            # attributes/object types) into a schema-level list, and record
            # which of them this attribute allows by name.
            status_option_names = None
            if type_string == "status" and attr.structure and isinstance(attr.structure, dict):
                raw_statuses = attr.structure.get("status_options")
                if raw_statuses:
                    status_option_names = []
                    for so in raw_statuses:
                        name = so.get("name")
                        if not name:
                            continue
                        status_option_names.append(name)
                        status_meta_by_name.setdefault(name, {
                            "name": name,
                            "color": so.get("color"),
                            "status_category": so.get("status_category"),
                        })

            attr_list.append({
                "name": attr.name,
                "type_string": type_string,
                "description": attr.description,
                "required": attr.is_required,
                "is_unique": attr.is_unique,
                "default_value": attr.default_value,
                "select_options": select_options,
                "status_option_names": status_option_names,
                "referenced_object_type_name": ref_name,
                "classification": template_classification(
                    attribute_classifications.get(attr.id)
                ),
            })

        icon_info = icon_map.get(ot.icon_id) if ot.icon_id else None
        ot_list.append({
            "name": ot.name,
            "description": ot.description,
            "icon_key": icon_info["name"] if icon_info else "box",
            "icon_type": icon_info["type"] if icon_info else None,
            "icon_url": icon_info["url"] if icon_info and icon_info["type"] != "lucide" else None,
            "parent_name": ot_id_to_name.get(ot.parent_id),
            "inherits_attributes": ot.inherits_attributes,
            "is_abstract": ot.is_abstract,
            "attributes": attr_list,
        })

    return {
        "schema_name": schema.name,
        "schema_description": schema.description,
        "object_types": ot_list,
        "statuses": list(status_meta_by_name.values()),
    }


async def list_schema_languages(db: AsyncSession, schema_id: uuid.UUID) -> List[Language]:
    result = await db.execute(
        select(Language)
        .join(SchemaLanguage, SchemaLanguage.language_id == Language.id)
        .where(SchemaLanguage.schema_id == schema_id)
        .order_by(Language.code)
    )
    return list(result.scalars().all())


async def set_schema_languages(
    db: AsyncSession, schema_id: uuid.UUID, language_codes: List[str], actor_id: uuid.UUID
) -> List[Language] | None:
    """Replace a schema's enabled language subset (D-12: codes, resolved server-side).

    D-08: inactive codes rejected (400). D-07: removing the current default → 409.
    Returns None if the schema does not exist."""
    schema_result = await db.execute(select(Schema).where(Schema.id == schema_id))
    schema = schema_result.scalar_one_or_none()
    if not schema:
        return None

    # Deduplicate codes — duplicate entries would violate uq_schema_languages_pair
    language_codes = list(dict.fromkeys(language_codes))

    langs_result = await db.execute(select(Language).where(Language.code.in_(language_codes)))
    langs_by_code = {lang.code: lang for lang in langs_result.scalars().all()}
    missing = [c for c in language_codes if c not in langs_by_code]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown language code(s): {', '.join(missing)}",
        )
    inactive = [c for c in language_codes if not langs_by_code[c].is_active]
    if inactive:
        raise HTTPException(
            status_code=400,
            detail=f"Inactive language code(s) cannot be enabled: {', '.join(inactive)}",
        )

    new_ids = {langs_by_code[c].id for c in language_codes}
    if schema.default_language_id is not None and schema.default_language_id not in new_ids:
        raise HTTPException(
            status_code=409,
            detail="Cannot remove the schema's default language from its enabled subset. Re-point or clear the default first.",
        )

    old_rows = await db.execute(select(SchemaLanguage).where(SchemaLanguage.schema_id == schema_id))
    old_codes = sorted(await _collect_existing_codes(db, schema_id))
    for row in old_rows.scalars().all():
        await db.delete(row)
    # Flush the DELETEs before inserting replacements — within a single flush
    # SQLAlchemy emits INSERTs before DELETEs, and a re-enabled code would
    # violate uq_schema_languages_pair (IntegrityError → 500).
    await db.flush()
    for code in language_codes:
        db.add(SchemaLanguage(schema_id=schema_id, language_id=langs_by_code[code].id))
    await audit_log(db, "schemas", schema_id, "UPDATE", actor_id,
                    old_values={"language_codes": old_codes}, new_values={"language_codes": language_codes})
    await db.commit()
    await invalidate_entity_cache("schema")  # D-13: existing mechanism, entity + list
    # 227 D-01: crossing the 1<->2 boundary flips multilinguality for the WHOLE
    # schema. Every translation-bearing cache family reads through gates keyed
    # to that state (resolve_translations filter + SchemaResponse.multilingual),
    # so all of them must drop — otherwise stale payloads serve across the flip.
    old_count = len(old_codes)
    new_count = len(language_codes)  # post-dedup (:672)
    if (old_count < 2) != (new_count < 2):
        for family in (
            "status", "reference_type", "object_type",
            "object_type_attribute", "object_value", "object",
        ):
            await invalidate_entity_cache(family)
    return await list_schema_languages(db, schema_id)


async def _collect_existing_codes(db: AsyncSession, schema_id: uuid.UUID) -> list[str]:
    result = await db.execute(
        select(Language.code)
        .join(SchemaLanguage, SchemaLanguage.language_id == Language.id)
        .where(SchemaLanguage.schema_id == schema_id)
    )
    return list(result.scalars().all())
