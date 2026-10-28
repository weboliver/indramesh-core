import base64
import json
import uuid
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select, func, and_, or_, case, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Language, Translation, UiString
from app.schemas.crud import UiDictionaryResponse
from app.services import translation_service
from app.services.cache_service import build_tag_key, get_cached, set_cached_with_tags
from app.services.translation_service import _resolve_language, get_translation_type_by_code

# Phase 230 / STOR-03: dedicated, schema-less resolver for systemui dictionary
# reads. Deliberately does NOT import or call the schema-scoped batch resolver,
# the schema-language-envelope gate, the schema-multilingual gate, or the
# entity-schema lookup helper — ui_strings rows have no owning schema (research
# Pitfall #2).


async def resolve_ui_dictionary(db: AsyncSession, lang_code: str) -> dict:
    """Resolve the per-language systemui dictionary: {"lang": code, "strings": {...}}.

    Chain per key: requested-language row (non-blank) -> en row (non-blank) ->
    UiString.default_text — never an empty string, never the raw key.
    Cached at ui_translations:dict:{language.code} (D-04: invalidate-on-write
    only, no TTL tuning, no version suffix).
    """
    # Validation FIRST — an unknown/inactive code can never allocate a cache
    # entry (cache-poisoning guard, T-230-05).
    language = await _resolve_language(db, lang_code)

    cache_key = f"ui_translations:dict:{language.code}"
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached

    systemui_type = await get_translation_type_by_code(db, "systemui")
    if not systemui_type:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="systemui translation type is not seeded")

    # English fallback WITHOUT the active-check — an admin deactivating "en"
    # must not break the fallback chain. If "en" doesn't exist at all, the en
    # layer is simply absent and the chain falls through to default_text.
    en_result = await db.execute(select(Language).where(Language.code == "en"))
    en_language = en_result.scalar_one_or_none()
    en_id = en_language.id if en_language else None

    ui_strings_result = await db.execute(select(UiString))
    ui_strings = ui_strings_result.scalars().all()

    lang_ids = {language.id}
    if en_id is not None:
        lang_ids.add(en_id)
    translations_result = await db.execute(
        select(Translation).where(
            Translation.translation_type_id == systemui_type.id,
            Translation.language_id.in_(lang_ids),
        )
    )
    by_entity: dict = {}
    for t in translations_result.scalars().all():
        by_entity.setdefault(t.entity_id, {})[t.language_id] = t.value

    strings: dict[str, str] = {}
    for ui_string in ui_strings:
        entity_values = by_entity.get(ui_string.id, {})
        requested_value = entity_values.get(language.id)
        en_value = entity_values.get(en_id) if en_id is not None else None
        if requested_value and requested_value.strip():
            strings[ui_string.key] = requested_value
        elif en_value and en_value.strip():
            strings[ui_string.key] = en_value
        else:
            strings[ui_string.key] = ui_string.default_text

    payload = UiDictionaryResponse(lang=language.code, strings=strings).model_dump()
    await set_cached_with_tags(cache_key, payload, [build_tag_key("ui_translations")], is_list=True)
    return payload


async def list_ui_keys(
    db: AsyncSession, skip: int = 0, limit: int = 100, namespace: str | None = None
) -> tuple[List[UiString], int]:
    count_query = select(func.count()).select_from(UiString)
    query = select(UiString).order_by(UiString.key).offset(skip).limit(limit)
    if namespace is not None:
        count_query = count_query.where(UiString.namespace == namespace)
        query = query.where(UiString.namespace == namespace)
    total = (await db.execute(count_query)).scalar()
    items = (await db.execute(query)).scalars().all()
    return list(items), total


def build_ui_catalog_filters(
    namespace: str | None,
    namespace_is_null: bool,
    q: str | None,
    selected_alias,
    english_alias,
    ui_string_id: uuid.UUID | None = None,
):
    """Shared namespace/NULL-namespace/q filter predicate + effective-value
    expression for the UiString catalog (Phase 233 D-01) and the AI bulk
    candidate/suggestion queries (Phase 234). Extracted so the catalog route
    and the candidate-discovery query cannot drift (234-PLAN-REVIEW.md).

    ``selected_alias``/``english_alias`` are the caller's outer-joined
    ``Translation`` aliases for the selected/target language and English
    respectively. Returns ``(apply_filters, effective_expr)`` where
    ``apply_filters(stmt)`` binds the namespace/q predicates onto any
    SQLAlchemy select touching ``UiString``, and ``effective_expr`` is the
    coalesced selected -> english -> default_text expression used by the q
    search (identical semantics to the pre-extraction closure).
    """
    effective_expr = func.coalesce(
        func.nullif(func.trim(selected_alias.value), ""),
        func.nullif(func.trim(english_alias.value), ""),
        UiString.default_text,
    )

    def apply_filters(stmt):
        if ui_string_id is not None:
            stmt = stmt.where(UiString.id == ui_string_id)
        if namespace_is_null:
            stmt = stmt.where(UiString.namespace.is_(None))
        elif namespace is not None:
            stmt = stmt.where(UiString.namespace == namespace)
        if q:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(UiString.key).like(pattern),
                    func.lower(effective_expr).like(pattern),
                )
            )
        return stmt

    return apply_filters, effective_expr


async def get_ui_translation_catalog(
    db: AsyncSession,
    editor_language: str,
    namespace: str | None = None,
    namespace_is_null: bool = False,
    q: str | None = None,
    skip: int = 0,
    limit: int = 100,
    ui_string_id: uuid.UUID | None = None,
    only_missing: bool = False,
) -> dict:
    """Joined, server-paginated systemui catalog page (Phase 233 / ADMIN-01/02).

    Outer-joins the selected editor language and English systemui/value rows
    to UiString, derives effective text selected -> English -> default_text,
    and reports fallback provenance separately from the raw stored value.
    Namespace and case-insensitive key/effective-value search are bound
    SQLAlchemy predicates applied identically to the count and page queries
    -- never a full-catalog materialization. Namespace options and
    active-language coverage (explicit non-whitespace rows only) are derived
    server-side from the full registry, independent of the current page.

    ``only_missing`` (default False, byte-identical existing behavior when
    omitted): adds the same "no explicit non-blank selected value" predicate
    the AI bulk candidate query uses, so the review block can be shown *as*
    this page (Phase 234 Plan 04 — up to 100 missing keys, one page).
    """
    language = await _resolve_language(db, editor_language)
    systemui_type = await get_translation_type_by_code(db, "systemui")
    if not systemui_type:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="systemui translation type is not seeded")

    en_result = await db.execute(select(Language).where(Language.code == "en"))
    en_language = en_result.scalar_one_or_none()
    en_id = en_language.id if en_language else None

    SelectedT = aliased(Translation)
    EnglishT = aliased(Translation)

    selected_on = and_(
        SelectedT.translation_type_id == systemui_type.id,
        SelectedT.entity_id == UiString.id,
        SelectedT.field == "value",
        SelectedT.language_id == language.id,
    )
    english_on = and_(
        EnglishT.translation_type_id == systemui_type.id,
        EnglishT.entity_id == UiString.id,
        EnglishT.field == "value",
        EnglishT.language_id == en_id,
    )

    apply_filters, effective_expr = build_ui_catalog_filters(
        namespace, namespace_is_null, q, SelectedT, EnglishT, ui_string_id
    )
    fallback_source_expr = case(
        (and_(SelectedT.value.isnot(None), func.trim(SelectedT.value) != ""), "explicit"),
        (and_(EnglishT.value.isnot(None), func.trim(EnglishT.value) != ""), "english"),
        else_="code_default",
    )

    def _apply_filters(stmt):
        stmt = apply_filters(stmt)
        if only_missing:
            stmt = stmt.where(or_(SelectedT.value.is_(None), func.trim(SelectedT.value) == ""))
        return stmt

    count_query = _apply_filters(
        select(func.count())
        .select_from(UiString)
        .outerjoin(SelectedT, selected_on)
        .outerjoin(EnglishT, english_on)
    )
    total = (await db.execute(count_query)).scalar()

    page_query = _apply_filters(
        select(
            UiString.id,
            UiString.key,
            UiString.namespace,
            UiString.default_text,
            SelectedT.value.label("stored_value"),
            effective_expr.label("effective_value"),
            fallback_source_expr.label("fallback_source"),
        )
        .outerjoin(SelectedT, selected_on)
        .outerjoin(EnglishT, english_on)
        .order_by(UiString.key)
        .offset(skip)
        .limit(limit)
    )
    rows = (await db.execute(page_query)).all()
    items = [
        {
            "id": row.id,
            "key": row.key,
            "namespace": row.namespace,
            "default_text": row.default_text,
            "stored_value": row.stored_value,
            "effective_value": row.effective_value,
            "fallback_source": row.fallback_source,
        }
        for row in rows
    ]

    ns_result = await db.execute(select(UiString.namespace).distinct())
    ns_values = {r[0] for r in ns_result.all()}
    namespace_options = [{"value": v} for v in sorted(v for v in ns_values if v is not None)]
    if None in ns_values:
        namespace_options.append({"value": None})

    active_langs_result = await db.execute(select(Language).where(Language.is_active.is_(True)))
    active_langs = active_langs_result.scalars().all()
    total_keys = (await db.execute(select(func.count()).select_from(UiString))).scalar()

    coverage = []
    for lang in active_langs:
        translated = (
            await db.execute(
                select(func.count())
                .select_from(Translation)
                .where(
                    Translation.translation_type_id == systemui_type.id,
                    Translation.field == "value",
                    Translation.language_id == lang.id,
                    func.trim(Translation.value) != "",
                )
            )
        ).scalar()
        coverage.append({"code": lang.code, "translated": translated, "total": total_keys})

    return {
        "items": items,
        "total": total,
        "namespaces": namespace_options,
        "coverage": coverage,
        "editor_language": language.code,
    }


async def apply_ui_translation_batch(
    db: AsyncSession,
    entries: list[dict],
    *,
    mode: str,
    actor_id: uuid.UUID,
    authorize: "Callable[[AsyncSession, object], Awaitable[None]] | None" = None,
) -> dict:
    """Atomic, explicit-mode SDK write for systemui translations (SEAM-04/D-11).

    A UI translation is the schema-less ``systemui``/``value`` row, so the write
    is delegated to the shared translation batch primitive — the same
    validation, fill/replace/skip contract and one-commit atomicity as the
    data-translation path. Each entry is ``{ui_string_id, language_code, value}``.
    ``authorize`` is forwarded to the shared batch (D-15).
    """
    mapped = [
        {
            "translation_type": "systemui",
            "entity_id": entry["ui_string_id"],
            "field": "value",
            "language_code": entry["language_code"],
            "value": entry["value"],
        }
        for entry in entries
    ]
    result = await translation_service.apply_translation_batch(
        db, mapped, mode=mode, actor_id=actor_id, authorize=authorize
    )
    # Re-key each result onto the UI identity so the caller sees the ui_string id.
    for entry_result, original in zip(result["results"], entries):
        entry_result["ui_string_id"] = original["ui_string_id"]
    return result


def _encode_ui_cursor(namespace: str | None, key: str, ui_string_id: uuid.UUID) -> str:
    payload = json.dumps([namespace, key, str(ui_string_id)]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_ui_cursor(cursor: str) -> tuple[str, str, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        namespace, key, raw_id = json.loads(raw)
        return (
            namespace if namespace is not None else "",
            str(key),
            uuid.UUID(str(raw_id)),
        )
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid UI catalogue cursor",
        ) from None


async def read_ui_catalog_page(
    db: AsyncSession,
    *,
    editor_language: str,
    namespace: str | None = None,
    namespace_is_null: bool = False,
    q: str | None = None,
    after: str | None = None,
    limit: int = 100,
) -> dict:
    """Keyset-paginated systemui catalogue page (SEAM-04, D-06/D-15).

    Uses the SAME namespace/NULL-namespace/q filter predicates as the REST
    catalogue (:func:`build_ui_catalog_filters`), but orders by
    ``namespace`` (``NULL`` first via ``COALESCE``), ``key`` then row UUID and
    advances by an opaque cursor instead of an offset. Returns the row UUID, the
    authored ``default_text`` and the raw stored value separately from the
    effective (fallback) value, plus the filter-wide ``total`` and the next
    cursor. No record-scoped authorization applies — ``systemui`` has no owning
    Core record (D-15).
    """
    language = await _resolve_language(db, editor_language)
    systemui_type = await get_translation_type_by_code(db, "systemui")
    if not systemui_type:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="systemui translation type is not seeded")

    en_result = await db.execute(select(Language).where(Language.code == "en"))
    en_language = en_result.scalar_one_or_none()
    en_id = en_language.id if en_language else None

    SelectedT = aliased(Translation)
    EnglishT = aliased(Translation)
    selected_on = and_(
        SelectedT.translation_type_id == systemui_type.id,
        SelectedT.entity_id == UiString.id,
        SelectedT.field == "value",
        SelectedT.language_id == language.id,
    )
    english_on = and_(
        EnglishT.translation_type_id == systemui_type.id,
        EnglishT.entity_id == UiString.id,
        EnglishT.field == "value",
        EnglishT.language_id == en_id,
    )
    apply_filters, effective_expr = build_ui_catalog_filters(
        namespace, namespace_is_null, q, SelectedT, EnglishT
    )
    fallback_source_expr = case(
        (and_(SelectedT.value.isnot(None), func.trim(SelectedT.value) != ""), "explicit"),
        (and_(EnglishT.value.isnot(None), func.trim(EnglishT.value) != ""), "english"),
        else_="code_default",
    )
    namespace_expr = func.coalesce(UiString.namespace, "")

    count_query = apply_filters(
        select(func.count())
        .select_from(UiString)
        .outerjoin(SelectedT, selected_on)
        .outerjoin(EnglishT, english_on)
    )
    total = (await db.execute(count_query)).scalar()

    page_query = apply_filters(
        select(
            UiString.id,
            UiString.key,
            UiString.namespace,
            UiString.default_text,
            SelectedT.value.label("stored_value"),
            effective_expr.label("effective_value"),
            fallback_source_expr.label("fallback_source"),
        )
        .outerjoin(SelectedT, selected_on)
        .outerjoin(EnglishT, english_on)
    )
    if after is not None:
        cursor_namespace, cursor_key, cursor_id = _decode_ui_cursor(after)
        page_query = page_query.where(
            tuple_(namespace_expr, UiString.key, UiString.id)
            > tuple_(cursor_namespace, cursor_key, cursor_id)
        )
    page_query = page_query.order_by(
        namespace_expr, UiString.key, UiString.id
    ).limit(limit + 1)

    rows = (await db.execute(page_query)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        {
            "id": row.id,
            "key": row.key,
            "namespace": row.namespace,
            "default_text": row.default_text,
            "stored_value": row.stored_value,
            "effective_value": row.effective_value,
            "fallback_source": row.fallback_source,
        }
        for row in page_rows
    ]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_ui_cursor(last.namespace, last.key, last.id)
    return {"items": items, "total": total, "next_cursor": next_cursor}
