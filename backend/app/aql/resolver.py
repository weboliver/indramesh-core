import uuid
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.attribute_type import AttributeType
from app.models.object import Object
from app.models.object_ref import ObjectRef
from app.models.object_type import ObjectType
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.reference_type import ReferenceType
from app.aql.schemas import CompileOutput, AQLResolvedQuery, HavingCondition
from app.aql.transformer import _transform_expression
from app.aql.errors import AQLValidationError
from app.services.filter_utils import evaluate_filter_tree
from app.services.object_type_service import get_descendant_type_ids
from app.services.record_permission_service import build_record_permission_filter
from app.services.select_option_service import resolve_select_literal
from app.services.value_visibility import (
    visible_attribute_conditions,
    visible_object_ids_subquery,
)


class AQLResolver:
    """Resolves human-readable AQL names to database UUIDs.

    Uses batch DB lookups to avoid N+1 queries during resolution.

    Bound to the request principal (D-05): the same value-visibility predicate
    the REST exits compose also decides which *names* this caller may resolve
    and which objects a relationship walk may materialise. Without a principal
    (``user_id is None``) the resolver keeps the pre-317 reading — production
    constructions always pass the caller from the AQL compiler.
    """

    def __init__(
        self,
        db: AsyncSession,
        user_id: str | None = None,
        is_superuser: bool = False,
        role_ids: list[uuid.UUID] | None = None,
    ):
        self.db = db
        self.user_id = user_id
        self.is_superuser = is_superuser
        self.role_ids = role_ids

    def _has_principal(self) -> bool:
        """True when the caller's visibility predicate must be applied."""
        return self.user_id is not None and not self.is_superuser

    async def _restrict_attribute_statement(self, stmt):
        """Join ``attribute_types`` and apply the attribute name-space predicate.

        A hidden OTA row or a record-DENY'd AttributeType simply stops matching,
        so the name falls into the caller's existing "unknown name" error: an
        invisible attribute answers exactly like a non-existent one instead of
        contributing a result count (SEC313-12, SEC313-19).
        """
        if not self._has_principal():
            return stmt
        clauses = await visible_attribute_conditions(
            self.db, self.user_id, self.is_superuser, role_ids=self.role_ids
        )
        return stmt.join(
            AttributeType,
            ObjectTypeAttribute.attribute_type_id == AttributeType.id,
        ).where(*clauses)

    async def _restrict_object_type_statement(self, stmt):
        """Apply the object-type-level record predicate to an ObjectType query.

        A type the caller may not read must answer exactly like a type that does
        not exist — never "exists but empty", which would turn the resolver into
        a metadata oracle for schemas the caller cannot see.
        """
        if not self._has_principal():
            return stmt
        clause = await build_record_permission_filter(
            self.db,
            self.user_id,
            "object_type",
            is_superuser=False,
            role_ids=self.role_ids,
        )
        if clause is None:
            return stmt
        return stmt.where(clause)

    async def _restrict_object_statement(self, stmt):
        """Intersect an Object-id query with the visible-object subquery."""
        if not self._has_principal():
            return stmt
        subquery = await visible_object_ids_subquery(
            self.db, self.user_id, self.is_superuser, role_ids=self.role_ids
        )
        return stmt.where(Object.id.in_(subquery))

    async def resolve(
        self,
        output: CompileOutput,
        inferred_object_type_id: uuid.UUID | None = None,
    ) -> AQLResolvedQuery:
        """Resolve all names in a CompileOutput to their database UUIDs.

        Steps:
        1. Resolve objectType condition → object_type_id UUID (or fall back to
           inferred_object_type_id, used by recursive having-condition resolution
           when the nested condition has no explicit objectType — Pitfall 3)
        2. Resolve attribute conditions → attr_id UUIDs (scoped to object_type),
           or globally/intersected across ObjectTypes when unresolved (D-01/D-02)
        3. D-03 (revised): having (inR/outR) conditions no longer require a
           resolved objectType — refType names resolve globally instead
        4. Split conditions into ES-routed (~) and SQL-routed (=, >, etc.)
        5. Recursively resolve having (inR/outR) conditions → having_pk_filters

        Args:
            output: CompileOutput to resolve.
            inferred_object_type_id: Used only when output.object_type is None —
                supplies an objectType inferred from an unambiguous refType
                (Task 1's _infer_nested_object_type). Ignored when output.object_type
                is explicitly set (explicit objectType always wins).

        Raises:
            AQLValidationError: If names cannot be resolved or D-01/D-03 violated
        """
        # ORDER BY only ever affects ordering for fields list_objects() knows how
        # to sort by (_SORT_COLUMNS in object_service.py) — reject anything else
        # instead of silently no-op'ing (falling back to the "name" default) on
        # an unsupported sort request.
        if output.order_by is not None and output.order_by not in ("name", "created", "updated"):
            raise AQLValidationError(
                f'ORDER BY only supports "name", "created", "updated", got "{output.order_by}"'
            )

        object_type_ids: list[uuid.UUID] | None = None
        if output.object_type is not None and output.object_type.names is not None:
            # AQL-09: objectType IN (...) — the transformer already rejects
            # combining it with attribute/having conditions (no single
            # ObjectType to scope attr/refType name resolution against), so
            # this branch never has D-01/D-03 work left to do below.
            object_type_ids = await self._resolve_object_type_ids(
                output.object_type.names
            )
            object_type_id = None
        elif output.object_type is not None and output.object_type.and_children_name is not None:
            # objectTypeAndChildren("X") — same reasoning/restriction as
            # objectType IN (...) above, just resolved via hierarchy expansion
            # instead of an explicit name list.
            object_type_ids = await self._resolve_object_type_and_children(
                output.object_type.and_children_name
            )
            object_type_id = None
        else:
            matched_ids = await self._resolve_object_type(output)
            if matched_ids is not None and len(matched_ids) > 1:
                # ObjectType names are only unique per schema — the same name in
                # several schemas resolves to all of them (same union semantics
                # as _resolve_reftypes_global), never an arbitrary pick or crash.
                return await self._resolve_ambiguous_object_type(output, matched_ids)
            object_type_id = matched_ids[0] if matched_ids else inferred_object_type_id

        # D-03 (revised): having (inR/outR) conditions no longer require a
        # resolved objectType — when object_type_id is None, refType IN (...)
        # name resolution falls through to a global (cross-schema) lookup
        # instead of raising (see _resolve_having's outer_schema_id branch
        # below and _resolve_reftypes_global).

        candidate_object_type_ids: list[uuid.UUID] | None = None

        if object_type_id is not None:
            # Existing single-scope path (unchanged).
            attr_map = {}
            if output.conditions:
                attr_map = await self._resolve_attributes(
                    object_type_id,
                    [c.attr_name for c in output.conditions],
                )

            resolved_ft = await self._resolve_filter_tree(
                output.filter_tree, attr_map, object_type_id
            )
        elif output.conditions:
            # D-01/D-02/D-04/D-06: global/intersection attribute-name
            # resolution when no objectType is resolved.
            attr_names = sorted({c.attr_name for c in output.conditions})

            result = await self.db.execute(
                await self._restrict_attribute_statement(
                    select(
                        ObjectTypeAttribute.object_type_id,
                        ObjectTypeAttribute.name,
                    ).where(ObjectTypeAttribute.name.in_(attr_names))
                )
            )
            by_name: dict[str, set[uuid.UUID]] = {}
            for ot_id, name in result.all():
                by_name.setdefault(name, set()).add(ot_id)

            unknown = [name for name in attr_names if name not in by_name]
            if unknown:
                raise AQLValidationError(
                    f"Unknown attribute name(s): {', '.join(sorted(set(unknown)))}. "
                    f"These attribute names match zero ObjectTypes."
                )

            # D-02: intersection across all attribute names in this AND-branch.
            # An empty intersection is a legitimate zero-result query, NOT an
            # error (Pitfall 3) — never combine this with the D-06 cutoff check.
            candidate_ids = set.intersection(*by_name.values()) if by_name else set()

            # D-06: fan-out cutoff. Strict `>` only — never combined with an
            # emptiness check (Pitfall 3).
            if len(candidate_ids) > settings.aql_multi_type_cutoff:
                raise AQLValidationError(
                    f"Attribute condition(s) match too many ObjectTypes "
                    f"(matched {len(candidate_ids)}, limit is "
                    f"{settings.aql_multi_type_cutoff})."
                )

            candidate_object_type_ids = sorted(candidate_ids)

            # D-04: no cross-type validation — each candidate ObjectType is
            # resolved to its own attr_id map and its own filter_tree, reusing
            # _resolve_attributes/_resolve_filter_tree unchanged, once per
            # candidate. Excludes `~` (is_text_search) conditions from the SQL
            # filter_tree — those stay ES-only (D-05, Plan 03) to avoid
            # double-applying them as ILIKE (Pitfall 2).
            candidate_groups = []
            for candidate_id in candidate_object_type_ids:
                candidate_attr_map = await self._resolve_attributes(
                    candidate_id, attr_names
                )
                # `~` stays in the per-candidate tree: it executes as SQL ILIKE
                # (AQL-CONTAINS-01). Stripping it here was only correct while the
                # OpenSearch pre-filter executed it, and that pre-filter matched
                # whole tokens only.
                candidate_ft = await self._resolve_filter_tree(
                    output.filter_tree, candidate_attr_map, candidate_id
                )
                candidate_groups.append(candidate_ft)

            resolved_ft = {"op": "OR", "conditions": [], "groups": candidate_groups}
        else:
            # No objectType, no attribute conditions (e.g. a bare having-only
            # query, already validated above) — nothing to resolve here.
            resolved_ft = await self._resolve_filter_tree(
                output.filter_tree, {}, object_type_id
            )

        # Classification only: `~` conditions are marked here for observability,
        # but the compiler no longer routes them to OpenSearch — they run as SQL
        # ILIKE inside filter_tree like every other condition (AQL-CONTAINS-01).
        es_conditions = [
            c for c in output.conditions
            if c.is_text_search  # ~ operator
        ]
        sql_conditions = [
            c for c in output.conditions
            if not c.is_text_search  # =, !=, >, <, >=, <= → SQL filter_tree
        ]

        # Recursively resolve having (inR/outR) leaves into anchor PK sets.
        having_pk_filters: list[set[uuid.UUID]] = []
        if output.having_conditions:
            # Pitfall 4: _get_schema_id() uses scalar_one() and crashes on
            # None — only call it when object_type_id is actually resolved;
            # otherwise fall through to global (cross-schema) refType
            # resolution inside _resolve_having (D-03 revised).
            outer_schema_id = (
                await self._get_schema_id(object_type_id)
                if object_type_id is not None
                else None
            )
            for having in output.having_conditions:
                having_pk_filters.append(
                    await self._resolve_having(having, outer_schema_id)
                )

        return AQLResolvedQuery(
            filter_tree=resolved_ft,
            object_type_id=object_type_id,
            order_by=output.order_by,
            order_direction=output.order_direction,
            es_conditions=es_conditions,
            sql_conditions=sql_conditions,
            having_pk_filters=having_pk_filters,
            object_type_ids=object_type_ids,
            candidate_object_type_ids=candidate_object_type_ids,
        )

    async def _resolve_object_type(
        self, output: CompileOutput
    ) -> list[uuid.UUID] | None:
        """Resolve objectType name to ALL matching UUIDs (one per schema owning
        that name). Returns None if no objectType filter."""
        if output.object_type is None:
            return None

        name = output.object_type.name
        result = await self.db.execute(
            await self._restrict_object_type_statement(
                select(ObjectType.id).where(ObjectType.name == name)
            )
        )
        ot_ids = list(result.scalars().all())
        if not ot_ids:
            raise AQLValidationError(
                f"Unknown objectType \"{name}\". No ObjectType found with this name."
            )
        return ot_ids

    async def _resolve_ambiguous_object_type(
        self, output: CompileOutput, object_type_ids: list[uuid.UUID]
    ) -> AQLResolvedQuery:
        """`objectType = "X"` where X names ObjectTypes in several schemas.

        The result is a hard `object_type_ids` filter over every match. Attribute
        conditions are resolved per matching ObjectType that owns all of them and
        materialized per type (so a negated condition can never leak objects of a
        sibling type), then unioned into one PK set passed via having_pk_filters.
        `~` conditions stay in those per-type trees as ILIKE — ES hybrid routing
        needs a single schema-scoped type, so es_conditions is left empty.
        """
        having_pk_filters: list[set[uuid.UUID]] = []

        if output.conditions:
            attr_names = sorted({c.attr_name for c in output.conditions})
            result = await self.db.execute(
                await self._restrict_attribute_statement(
                    select(ObjectTypeAttribute.object_type_id, ObjectTypeAttribute.name)
                    .where(
                        ObjectTypeAttribute.object_type_id.in_(object_type_ids),
                        ObjectTypeAttribute.name.in_(attr_names),
                    )
                )
            )
            owned: dict[uuid.UUID, set[str]] = {}
            for ot_id, name in result.all():
                owned.setdefault(ot_id, set()).add(name)

            found = set().union(*owned.values()) if owned else set()
            unknown = [name for name in attr_names if name not in found]
            if unknown:
                raise AQLValidationError(
                    f"Unknown attribute name(s) for the resolved objectType: "
                    f"{', '.join(unknown)}. "
                    f"These attributes do not exist on this ObjectType."
                )

            matched_pks: set[uuid.UUID] = set()
            for ot_id in object_type_ids:
                if owned.get(ot_id, set()) != set(attr_names):
                    continue
                attr_map = await self._resolve_attributes(ot_id, attr_names)
                ot_tree = await self._resolve_filter_tree(output.filter_tree, attr_map, ot_id)
                matched_pks |= await self._materialize_pk_set(ot_id, ot_tree)
            having_pk_filters.append(matched_pks)

        for having in output.having_conditions:
            having_pk_filters.append(await self._resolve_having(having, None))

        return AQLResolvedQuery(
            filter_tree={"op": "AND", "conditions": [], "groups": []},
            object_type_id=None,
            order_by=output.order_by,
            order_direction=output.order_direction,
            es_conditions=[],
            sql_conditions=list(output.conditions),
            having_pk_filters=having_pk_filters,
            object_type_ids=object_type_ids,
        )

    async def _resolve_object_type_ids(self, names: list[str]) -> list[uuid.UUID]:
        """Batch-resolve `objectType IN (...)` names to UUIDs (AQL-09).

        A name existing in several schemas contributes every matching UUID.
        Raises AQLValidationError listing all unknown names at once (never
        per-name queries), mirroring _resolve_reftypes.
        """
        result = await self.db.execute(
            await self._restrict_object_type_statement(
                select(ObjectType.id, ObjectType.name).where(
                    ObjectType.name.in_(names)
                )
            )
        )
        ids_by_name: dict[str, list[uuid.UUID]] = {}
        for oid, name in result.all():
            ids_by_name.setdefault(name, []).append(oid)

        unknown = [name for name in names if name not in ids_by_name]
        if unknown:
            raise AQLValidationError(
                f"Unknown objectType name(s): {', '.join(sorted(set(unknown)))}. "
                "No ObjectType found with these names."
            )

        # Preserve query order but dedupe (objectType IN ("A", "A") should not
        # produce a duplicate UUID in the IN-list passed to list_objects()).
        seen: dict[uuid.UUID, None] = {}
        for name in names:
            for oid in ids_by_name[name]:
                seen[oid] = None
        return list(seen)

    async def _resolve_object_type_and_children(self, name: str) -> list[uuid.UUID]:
        """Resolve `objectTypeAndChildren("X")` to every X's UUID (one per schema
        owning that name) plus all descendant ObjectType UUIDs, reusing the same
        cycle-safe recursive CTE the regular REST object list uses to *exclude*
        descendants (object_service.py) — here we deliberately *include* them.
        """
        result = await self.db.execute(
            await self._restrict_object_type_statement(
                select(ObjectType.id).where(ObjectType.name == name)
            )
        )
        base_ids = list(result.scalars().all())
        if not base_ids:
            raise AQLValidationError(
                f'Unknown objectType "{name}". No ObjectType found with this name.'
            )
        seen: dict[uuid.UUID, None] = {}
        for base_id in base_ids:
            seen[base_id] = None
            for descendant_id in await get_descendant_type_ids(self.db, base_id):
                seen[descendant_id] = None
        return list(seen)

    async def _resolve_attributes(
        self, object_type_id: uuid.UUID, attr_names: list[str]
    ) -> dict[str, uuid.UUID]:
        """Batch-resolve attribute names to attr_id UUIDs scoped to an objectType.

        Returns dict mapping attribute_name → attr_id UUID.
        Raises AQLValidationError if any attribute name is unknown.
        """
        # Single batch query — NOT N+1. The visibility predicate drops hidden and
        # record-DENY'd attribute rows here, so their names land in `unknown`.
        result = await self.db.execute(
            await self._restrict_attribute_statement(
                select(ObjectTypeAttribute).where(
                    ObjectTypeAttribute.object_type_id == object_type_id,
                    ObjectTypeAttribute.name.in_(attr_names),
                )
            )
        )
        rows = result.scalars().all()

        found = {row.name: row.id for row in rows}

        # Check for unknown attribute names
        unknown = [name for name in attr_names if name not in found]
        if unknown:
            raise AQLValidationError(
                f"Unknown attribute name(s) for the resolved objectType: "
                f"{', '.join(sorted(set(unknown)))}. "
                f"These attributes do not exist on this ObjectType."
            )

        return found

    async def _resolve_filter_tree(
        self, tree: dict,
        attr_map: dict[str, uuid.UUID],
        object_type_id: uuid.UUID | None,
    ) -> dict:
        """Recursively walk filter_tree and replace attr_name with attr_id.

        This does NOT include the objectType condition (it's already extracted).
        Returns a filter_tree dict with UUIDs, ready for filter_utils.
        """
        # D-05: batch-load the structures of every attribute in scope (single
        # query, not per-condition) so SELECT condition literals can resolve
        # display strings -> option ids here — with AQL validation semantics
        # (D-06) — before the filter-tree path ever sees the value.
        select_lookups: dict[uuid.UUID, dict | None] = {}
        if attr_map:
            result = await self.db.execute(
                select(ObjectTypeAttribute)
                .options(selectinload(ObjectTypeAttribute.attribute_type))
                .where(ObjectTypeAttribute.id.in_(set(attr_map.values())))
            )
            for attr in result.scalars():
                if (
                    attr.attribute_type
                    and attr.attribute_type.data_type == "select"
                ):
                    select_lookups[attr.id] = attr.structure

        resolved_conditions = []
        for cond in tree.get("conditions", []):
            rc = dict(cond)
            if rc.get("type") == "attribute":
                attr_name = rc.pop("attr_name", "")
                if attr_name in attr_map:
                    rc["attr_id"] = str(attr_map[attr_name])
                else:
                    # Attribute not in this objectType scope — should not happen
                    # since _resolve_attributes validates, but handle gracefully
                    raise AQLValidationError(
                        f"Attribute \"{attr_name}\" could not be resolved"
                    )
                # Map AQL operator to filter_tree operator (D-02)
                aql_op = rc.get("op", "")
                ft_op = self._map_operator(aql_op)
                rc["op"] = ft_op
                # Handle != → eq + negate. XOR with any existing negate flag from a
                # NOT wrap — `rc["negate"] = True` unconditionally (as originally
                # written) silently drops a surrounding NOT, so `NOT "OS" != "Linux"`
                # (which should cancel to OS == Linux) would incorrectly stay negated.
                if aql_op == "!=":
                    rc["negate"] = not rc.get("negate", False)
                # filter_tree conditions read the leaf value via "val" (filter_utils.py),
                # not "value" (the transformer's intermediate key) — rename here.
                rc["val"] = rc.pop("value", None)
                # D-05/D-06: SELECT literals are display strings — resolve to the
                # canonical option id here, with AQL validation semantics: an
                # unknown value is a validation error naming attribute + value
                # (mirrors the unknown-name message style). The filter-tree path
                # (task-1 wiring in filter_utils) then sees an id and passes it
                # through idempotently. Base values only (D-07) — translation
                # rows are never consulted.
                structure = select_lookups.get(attr_map[attr_name])
                if structure is not None and rc.get("val") is not None:
                    literal = str(rc["val"])
                    try:
                        rc["val"] = resolve_select_literal(
                            structure, literal, attr_name=attr_name
                        )
                    except ValueError:
                        raise AQLValidationError(
                            f"Unknown option '{literal}' for attribute '{attr_name}'"
                        )
                # Remove transient fields
                rc.pop("type", None)
                resolved_conditions.append(rc)
            elif rc.get("type") == "objecttype":
                # objectType conditions are resolved metadata — skip in filter_tree
                continue
            elif rc.get("type") == "having":
                # having (object HAVING inR/outR) leaves are resolved into a hard
                # PK-set intersection (having_pk_filters), never left in filter_tree
                # — mirrors the objecttype skip above.
                continue
            else:
                resolved_conditions.append(rc)

        resolved_groups = []
        for group in tree.get("groups", []):
            resolved_groups.append(
                await self._resolve_filter_tree(group, attr_map, object_type_id)
            )

        return {
            "op": tree.get("op", "AND"),
            "conditions": resolved_conditions,
            "groups": resolved_groups,
        }

    def _map_operator(self, aql_op: str) -> str:
        """Map AQL operator string to filter_tree operator string (per D-02)."""
        mapping = {
            "=": "eq",
            "!=": "eq",      # negated via separate negate flag
            ">": "gt",
            "<": "lt",
            ">=": "gte",
            "<=": "lte",
            "~": "contains",
        }
        return mapping.get(aql_op, aql_op)

    async def _get_schema_id(self, object_type_id: uuid.UUID) -> uuid.UUID:
        """Fetch the schema_id owning a resolved ObjectType (D-03 scoping)."""
        result = await self.db.execute(
            select(ObjectType.schema_id).where(ObjectType.id == object_type_id)
        )
        return result.scalar_one()

    async def _resolve_reftypes(
        self, schema_id: uuid.UUID, reftype_names: list[str]
    ) -> list[uuid.UUID]:
        """Batch-resolve refType IN (...) names to ReferenceType UUIDs, scoped to
        schema_id (D-03 — never a global lookup).

        Raises AQLValidationError listing all unknown names at once (never
        per-name queries), and for any name that resolves to >1 row within the
        schema (defense-in-depth per D-04, even if the migration's UNIQUE
        constraint hasn't been applied in some environment).
        """
        result = await self.db.execute(
            select(ReferenceType.id, ReferenceType.reference).where(
                ReferenceType.reference.in_(reftype_names),
                ReferenceType.schema_id == schema_id,
            )
        )
        ids_by_name: dict[str, list[uuid.UUID]] = {}
        for rid, rname in result.all():
            ids_by_name.setdefault(rname, []).append(rid)

        unknown = [name for name in reftype_names if name not in ids_by_name]
        if unknown:
            raise AQLValidationError(
                f"Unknown refType name(s) for this schema: "
                f"{', '.join(sorted(set(unknown)))}. These ReferenceTypes do not "
                f"exist within the resolved objectType's schema."
            )

        ambiguous = [name for name, ids in ids_by_name.items() if len(ids) > 1]
        if ambiguous:
            raise AQLValidationError(
                f"refType name(s) resolve to multiple ReferenceType rows within "
                f"this schema: {', '.join(sorted(ambiguous))}. This should not "
                f"happen once the schema-scoped UNIQUE(schema_id, reference) "
                f"constraint is applied — contact an administrator."
            )

        return [ids_by_name[name][0] for name in reftype_names]

    async def _resolve_reftypes_global(
        self, reftype_names: list[str]
    ) -> list[uuid.UUID]:
        """Batch-resolve refType IN (...) names to ReferenceType UUIDs GLOBALLY,
        across ALL schemas (D-03 revised — used when no outer objectType is
        resolved, so there is no single schema_id to scope against).

        Unlike the schema-scoped _resolve_reftypes, this does NOT raise on a
        name matching multiple rows — a refType name legitimately existing in
        2+ schemas resolves to the UNION of all matching ReferenceType ids
        (Pitfall 1: the per-schema ">1 row = ambiguous" check is a defense-in-
        depth artifact of the schema-scoped UNIQUE(schema_id, reference)
        constraint and does NOT generalize to global scope).

        Raises AQLValidationError listing all unknown names at once (never
        per-name queries) only for names matching ZERO rows anywhere.
        """
        result = await self.db.execute(
            select(ReferenceType.id, ReferenceType.reference).where(
                ReferenceType.reference.in_(reftype_names),
            )
        )
        ids_by_name: dict[str, list[uuid.UUID]] = {}
        for rid, rname in result.all():
            ids_by_name.setdefault(rname, []).append(rid)

        unknown = [name for name in reftype_names if name not in ids_by_name]
        if unknown:
            raise AQLValidationError(
                f"Unknown refType name(s): "
                f"{', '.join(sorted(set(unknown)))}. These ReferenceTypes do "
                f"not exist in any schema."
            )

        all_ids: list[uuid.UUID] = []
        for name in reftype_names:
            all_ids.extend(ids_by_name[name])
        return all_ids

    async def _infer_nested_object_type(
        self, reftype_ids: list[uuid.UUID]
    ) -> uuid.UUID | None:
        """Infer a nested condition's implicit objectType from an unambiguous
        refType selection (Pitfall 3): if every matching ObjectTypeAttribute row
        shares one referenced_object_type_id, return it; otherwise (no reftype_ids,
        or multiple distinct target ObjectTypes) return None (ambiguous/impossible).
        """
        if not reftype_ids:
            return None
        result = await self.db.execute(
            select(ObjectTypeAttribute.referenced_object_type_id)
            .where(ObjectTypeAttribute.reference_type_id.in_(reftype_ids))
            .distinct()
        )
        distinct_ids = {rid for rid in result.scalars().all() if rid is not None}
        if len(distinct_ids) == 1:
            return distinct_ids.pop()
        return None

    async def _materialize_pk_set(
        self, object_type_id: uuid.UUID, filter_tree: dict
    ) -> set[uuid.UUID]:
        """Materialize a resolved filter_tree (already attr_id-resolved) into the
        set of matching Object PKs, scoped to object_type_id.

        The object-level predicate is applied **here**, not only at the final
        anchor query (D-05): this set is the nested side of an `inR()`/`outR()`
        traversal and the `aql_ref_cutoff` message reports its size, so an
        unfiltered set would let the caller count objects it may not read.
        """
        query = select(Object.id).where(Object.object_type_id == object_type_id)
        if filter_tree and (filter_tree.get("conditions") or filter_tree.get("groups")):
            clause = await evaluate_filter_tree(filter_tree, self.db)
            if clause is not None:
                query = query.where(clause)
        result = await self.db.execute(await self._restrict_object_statement(query))
        return set(result.scalars().all())

    async def _materialize_pk_set_multi(
        self, object_type_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Materialize the set of matching Object PKs scoped to MULTIPLE
        ObjectTypes at once (objectType IN (...) / objectTypeAndChildren(...)
        used inside inR()/outR()). No filter_tree argument — the transformer
        already rejects combining either multi-type form with attribute
        conditions, so there is never anything else to filter on here.

        Carries the same object-level predicate as ``_materialize_pk_set``.
        """
        query = select(Object.id).where(Object.object_type_id.in_(object_type_ids))
        result = await self.db.execute(await self._restrict_object_statement(query))
        return set(result.scalars().all())

    async def _resolve_having(
        self, having: HavingCondition, outer_schema_id: uuid.UUID | None
    ) -> set[uuid.UUID]:
        """Recursively resolve a single `object HAVING inR/outR(...)` leaf into
        its anchor object PK set (D-05 preprocessing step).

        1. Resolve refType names (if any), scoped to the OUTER query's schema_id
           — or, when outer_schema_id is None (D-03 revised: no outer objectType
           resolved), resolved globally across ALL schemas instead (Pitfall 1:
           a name legitimately existing in 2+ schemas is a UNION, not an
           ambiguity error).
        2. Recursively transform+resolve the nested condition (its own objectType,
           if given; else inferred from an unambiguous refType — Pitfall 3).
        3. Materialize the nested condition's PK set, composing in any further
           nested having_pk_filters (D-02 multi-hop).
        4. Enforce the aql_ref_cutoff DoS guard (D-06).
        5. Run ONE batched ObjectRef traversal query using the empirically-locked
           column-direction contract from Plan 02 (test_aql_reference_direction.py):
           inR anchor = to_object_id, matched against from_object_id;
           outR anchor = from_object_id, matched against to_object_id.
        """
        reftype_ids: list[uuid.UUID] = []
        if having.reftype_names:
            if outer_schema_id is not None:
                reftype_ids = await self._resolve_reftypes(
                    outer_schema_id, having.reftype_names
                )
            else:
                reftype_ids = await self._resolve_reftypes_global(
                    having.reftype_names
                )

        nested_output = _transform_expression(having.nested_tree)

        inferred_object_type_id: uuid.UUID | None = None
        if nested_output.object_type is None:
            inferred_object_type_id = await self._infer_nested_object_type(reftype_ids)
            if inferred_object_type_id is None and nested_output.conditions:
                raise AQLValidationError(
                    "Nested condition inside inR()/outR() has attribute conditions "
                    "but no resolvable objectType (refType omitted or ambiguous "
                    "across multiple target ObjectTypes). Add objectType = \"...\" "
                    "inside inR()/outR()."
                )

        nested_resolved = await self.resolve(
            nested_output, inferred_object_type_id=inferred_object_type_id
        )

        if nested_resolved.object_type_id is not None:
            # Single-type nested condition (objectType = "X", or inferred from an
            # unambiguous refType) — may carry attribute/having conditions of its own.
            nested_pks = await self._materialize_pk_set(
                nested_resolved.object_type_id, nested_resolved.filter_tree
            )
            # Compose further-nested inR/outR PK sets (multi-hop, D-02) via
            # intersection — same AND-narrowing semantics as Pattern 4.
            for inner_pk_set in nested_resolved.having_pk_filters:
                nested_pks &= inner_pk_set
        elif nested_resolved.object_type_ids is not None:
            # objectType IN (...) / objectTypeAndChildren(...) nested inside
            # inR()/outR() — the transformer already guarantees no attribute or
            # further having conditions accompany it, so a plain multi-type PK
            # scan is the whole story here.
            nested_pks = await self._materialize_pk_set_multi(nested_resolved.object_type_ids)
            # An ambiguous `objectType = "X"` carries its attribute/having
            # narrowing as PK sets here (see _resolve_ambiguous_object_type).
            for inner_pk_set in nested_resolved.having_pk_filters:
                nested_pks &= inner_pk_set
        else:
            raise AQLValidationError(
                "Nested condition inside inR()/outR() could not resolve an "
                "objectType. Add objectType = \"...\" inside inR()/outR(), or use "
                "refType IN (...) so it can be inferred."
            )

        if len(nested_pks) > settings.aql_ref_cutoff:
            raise AQLValidationError(
                f"Inner condition for inR()/outR() is too broad for reference "
                f"traversal (matched {len(nested_pks)} objects, limit is "
                f"{settings.aql_ref_cutoff})."
            )

        if not nested_pks:
            return set()

        nested_pk_list = list(nested_pks)

        if having.direction == "in":
            anchor_col = ObjectRef.to_object_id
            match_col = ObjectRef.from_object_id
        else:
            anchor_col = ObjectRef.from_object_id
            match_col = ObjectRef.to_object_id

        # Bound as a Python list → compiles to `= ANY(...)`, NOT a literal IN
        # list (Pitfall 2) — single batched query, never one per nested PK.
        query = select(anchor_col).where(match_col.in_(nested_pk_list)).distinct()
        if reftype_ids:
            query = query.join(
                ObjectTypeAttribute,
                and_(
                    ObjectTypeAttribute.object_type_id == ObjectRef.object_type_id,
                    ObjectTypeAttribute.name == ObjectRef.from_attribute_name,
                ),
            ).where(ObjectTypeAttribute.reference_type_id.in_(reftype_ids))

        result = await self.db.execute(query)
        return set(result.scalars().all())
