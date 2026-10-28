import uuid

from sqlalchemy import select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import ObjectType, ObjectTypeAttribute, Object, ObjectRef
from app.schemas.crud import GraphNode, GraphEdge, GraphResponse, \
    ObjectGraphNode, ObjectGraphEdge, ObjectGraphResponse
from app.services.record_permission_service import build_record_permission_filter


# Flag-aware effective-ancestor CTE (D-09/D-10, parent-side per D-06):
# the ancestor walk continues upward only while the JOINED PARENT's
# inherits_attributes is true — a parent propagates its attributes to its
# children when the parent itself carries the native flag. Both SELECT
# lists carry parent_id — the recursive join reads it from the CTE's own
# working table (omitting it raises "column ea.parent_id does not exist").
_EFF_ANCESTORS_SQL = """
WITH RECURSIVE eff_ancestors AS (
    SELECT id AS anchor_id, id AS ancestor_id, parent_id, inherits_attributes, 0 AS depth
    FROM object_types
    WHERE id = ANY(:node_ids)
    UNION ALL
    SELECT ea.anchor_id, ot.id, ot.parent_id, ot.inherits_attributes, ea.depth + 1
    FROM object_types ot
    INNER JOIN eff_ancestors ea ON ot.id = ea.parent_id
    WHERE ot.inherits_attributes = true AND ea.depth < 100
)
SELECT anchor_id, ancestor_id FROM eff_ancestors
"""

# Mirrored direction (incoming, A5): a defining type's attributes are effective
# on itself plus descendants where every parent link has inherits_attributes=true.
_INH_DESC_SQL = """
WITH RECURSIVE inh_desc AS (
    SELECT id, inherits_attributes, 0 AS depth FROM object_types
    WHERE id = ANY(:defining_ids)
    UNION ALL
    SELECT ot.id, ot.inherits_attributes, d.depth + 1
    FROM object_types ot
    INNER JOIN inh_desc d ON ot.parent_id = d.id
    WHERE d.inherits_attributes = true AND d.depth < 100
)
SELECT id FROM inh_desc
"""


async def _effective_ancestors(
    db: AsyncSession, node_ids: list[uuid.UUID],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """anchor_id -> set of effective ancestor ids (flag-aware, D-10)."""
    result = await db.execute(text(_EFF_ANCESTORS_SQL), {"node_ids": node_ids})
    owner_map: dict[uuid.UUID, set[uuid.UUID]] = {}
    for row in result.all():
        owner_map.setdefault(row[0], set()).add(row[1])
    return owner_map


_ENTITY_MODEL = {"object_type": ObjectType, "object": Object}


async def _filter_visible_ids(
    db: AsyncSession,
    candidate_ids: set[uuid.UUID],
    user_id: str | None,
    is_superuser: bool,
    entity_type: str = "object_type",
) -> set[uuid.UUID]:
    """Visibility-filter a candidate id set (PITFALLS-1 pattern, D-15/D-16)."""
    if user_id is None or not candidate_ids:
        return candidate_ids
    filter_clause = await build_record_permission_filter(
        db, user_id, entity_type, is_superuser=is_superuser
    )
    if filter_clause is None:
        return candidate_ids
    model = _ENTITY_MODEL[entity_type]
    res = await db.execute(
        select(model.id)
        .where(model.id.in_(candidate_ids))
        .where(filter_clause)
    )
    return {row[0] for row in res.all()}


async def get_object_type_graph(
    db: AsyncSession,
    object_type_id: uuid.UUID,
    user_id: str | None = None,
    is_superuser: bool = False,
) -> GraphResponse | None:
    anchor = (
        await db.execute(select(ObjectType.id).where(ObjectType.id == object_type_id))
    ).scalar_one_or_none()
    if anchor is None:
        return None

    cutoff = settings.object_type_graph_cutoff
    all_visited: set[uuid.UUID] = {object_type_id}
    returned_nodes: list[uuid.UUID] = [object_type_id]
    frontier: list[uuid.UUID] = [object_type_id]

    # Direct-connections contract (Phase 218 user decision): only the anchor's
    # DIRECT reference neighbors are returned (one hop, outgoing + incoming).
    # No transitive BFS — deeper hops are intentionally excluded so densely
    # connected schemas do not collapse into one giant graph per anchor.
    while frontier:
        owner_map = await _effective_ancestors(db, frontier)
        ancestor_ids = {a for anc in owner_map.values() for a in anc}

        # Outgoing — reference attributes owned by the frontier's effective set.
        ota_result = await db.execute(
            select(ObjectTypeAttribute.referenced_object_type_id).where(
                ObjectTypeAttribute.object_type_id.in_(ancestor_ids),
                ObjectTypeAttribute.referenced_object_type_id.is_not(None),
            )
        )
        outgoing_candidates = {row[0] for row in ota_result.all()}

        # Incoming — defining types of OTAs referencing the frontier, plus
        # their inheriting descendants (A5, mirrored direction).
        in_result = await db.execute(
            select(ObjectTypeAttribute.object_type_id).where(
                ObjectTypeAttribute.referenced_object_type_id.in_(frontier)
            )
        )
        defining_ids = {row[0] for row in in_result.all()}
        incoming_candidates = set(defining_ids)
        if defining_ids:
            desc_result = await db.execute(
                text(_INH_DESC_SQL), {"defining_ids": list(defining_ids)}
            )
            incoming_candidates = defining_ids | {row[0] for row in desc_result.all()}

        candidates = outgoing_candidates | incoming_candidates
        visible_ids = await _filter_visible_ids(db, candidates, user_id, is_superuser)
        new_ids = visible_ids - all_visited
        if not new_ids:
            frontier = []
            break

        if len(returned_nodes) < cutoff:
            order_result = await db.execute(
                select(ObjectType.id, ObjectType.sort, ObjectType.name).where(
                    ObjectType.id.in_(new_ids)
                )
            )
            new_sorted = [
                row[0]
                for row in sorted(
                    order_result.all(), key=lambda r: (r[1] or 0, r[2] or "")
                )
            ]
        else:
            new_sorted = sorted(new_ids)

        for nid in new_sorted:
            all_visited.add(nid)
            if len(returned_nodes) < cutoff:
                returned_nodes.append(nid)
        frontier = []  # depth-1: stop after the direct hop

    total_count = len(all_visited)
    truncated = total_count > cutoff

    # D-05 hybrid anchor: fully isolated anchor returns the empty payload.
    nodes: list[GraphNode] = []
    edge_list: list[GraphEdge] = []
    if returned_nodes:
        node_result = await db.execute(
            select(ObjectType)
            .options(selectinload(ObjectType.icon))
            .where(ObjectType.id.in_(returned_nodes))
        )
        node_map = {ot.id: ot for ot in node_result.scalars().all()}
        nodes = [
            GraphNode(
                id=ot.id,
                name=ot.name,
                icon_local_name=ot.icon_local_name,
                schema_id=ot.schema_id,
                is_anchor=(ot.id == object_type_id),
            )
            for ot in (node_map[nid] for nid in returned_nodes)
        ]

        owner_map = await _effective_ancestors(db, returned_nodes)
        ancestor_ids = {a for anc in owner_map.values() for a in anc}
        owners_by_defining: dict[uuid.UUID, list[uuid.UUID]] = {}
        for anchor_id, ancestors in owner_map.items():
            for anc in ancestors:
                owners_by_defining.setdefault(anc, []).append(anchor_id)

        ota_result = await db.execute(
            select(ObjectTypeAttribute)
            .options(selectinload(ObjectTypeAttribute.reference_type))
            .where(
                ObjectTypeAttribute.object_type_id.in_(ancestor_ids),
                ObjectTypeAttribute.referenced_object_type_id.is_not(None),
            )
        )
        ota_rows = ota_result.scalars().all()

        # Defining-type visibility check (Phase 194 precedent: a DENY on the
        # defining type excludes the inherited attribute).
        defining_ids = {ota.object_type_id for ota in ota_rows}
        visible_defining_ids = await _filter_visible_ids(
            db, defining_ids, user_id, is_superuser
        )

        returned_set = set(returned_nodes)
        for ota in ota_rows:
            defining = ota.object_type_id
            target = ota.referenced_object_type_id
            if defining not in visible_defining_ids:
                continue
            if target not in returned_set:
                continue  # D-13: no dangling edges to omitted nodes
            shared = {
                "target": target,
                "attribute_id": ota.id,
                "attribute_name": ota.name,
                "reference_type_id": ota.reference_type_id,
                "reference_type_color": ota.reference_type.color if ota.reference_type else None,
            }
            if defining in returned_set:
                # Direct edge wins (Pitfall-4 lock).
                edge_list.append(
                    GraphEdge(id=str(ota.id), source=defining, is_inherited=False, **shared)
                )
            else:
                # One inherited edge per in-set effective owner with the
                # deterministic ':source' suffix.
                for owner in owners_by_defining.get(defining, []):
                    edge_list.append(
                        GraphEdge(
                            id=f"{ota.id}:{owner}", source=owner, is_inherited=True, **shared
                        )
                    )

        edge_list.sort(key=lambda e: (e.attribute_name, e.source, e.target))

    if total_count == 1 and not edge_list:
        return GraphResponse(nodes=[], edges=[], truncated=False, totalCount=0)

    return GraphResponse(
        nodes=nodes,
        edges=edge_list,
        truncated=truncated,
        totalCount=total_count,
    )


async def get_object_graph(
    db: AsyncSession,
    object_id: uuid.UUID,
    user_id: str | None = None,
    is_superuser: bool = False,
) -> ObjectGraphResponse | None:
    anchor = (
        await db.execute(select(Object.id).where(Object.id == object_id))
    ).scalar_one_or_none()
    if anchor is None:
        return None

    cutoff = settings.object_graph_cutoff
    ref_result = await db.execute(
        select(ObjectRef.id, ObjectRef.from_object_id, ObjectRef.to_object_id,
               ObjectRef.from_attribute_name).where(
            or_(ObjectRef.from_object_id == object_id,
                ObjectRef.to_object_id == object_id)
        )
    )
    ref_rows = ref_result.all()

    candidates = {
        r[1] if r[2] == object_id else r[2] for r in ref_rows if r[1] != r[2]
    }
    visible_ids = await _filter_visible_ids(
        db, candidates, user_id, is_superuser, entity_type="object"
    )
    total_count = len(visible_ids)
    truncated = total_count > cutoff

    node_query = (
        select(Object)
        .options(selectinload(Object.object_type))
        .where(Object.id.in_(visible_ids))
        .order_by(Object.name, Object.id)
    )
    if truncated:
        node_query = node_query.limit(cutoff)
    neighbor_rows = (await db.execute(node_query)).scalars().all()
    returned_neighbor_ids = [o.id for o in neighbor_rows]

    returned_set = set(returned_neighbor_ids) | {object_id}
    edge_list = [
        ObjectGraphEdge(
            id=str(r[0]),
            source=r[1],
            target=r[2],
            from_attribute_name=r[3],
        )
        for r in ref_rows
        if r[1] in returned_set and r[2] in returned_set
    ]
    edge_list.sort(key=lambda e: (e.from_attribute_name or "", e.source, e.target))

    if total_count == 0 and not edge_list:
        return ObjectGraphResponse(nodes=[], edges=[], truncated=False, totalCount=0)

    anchor_row = (
        await db.execute(
            select(Object).options(selectinload(Object.object_type))
            .where(Object.id == object_id)
        )
    ).scalar_one()

    nodes = [
        ObjectGraphNode(
            id=o.id,
            name=o.name,
            object_type_id=o.object_type_id,
            schema_id=o.schema_id,
            object_type_name=o.object_type.name if o.object_type else None,
            icon_local_name=o.object_type.icon_local_name if o.object_type else None,
            is_anchor=(o.id == object_id),
        )
        for o in [anchor_row] + neighbor_rows
    ]

    return ObjectGraphResponse(
        nodes=nodes,
        edges=edge_list,
        truncated=truncated,
        totalCount=total_count,
    )
