"""Pure Jira-Object-shape mapping for the public read path (READ-01).

Turns assembled read data (plain dicts, see ObjectReadData) into the exact
Jira `Object` wire shape pinned by the golden fixture contract
(backend/tests/fixtures/jira-object-1842.json). Explicit allow-list dict
construction — the mapper emits ONLY golden-shaped keys; no ORM
serialization anywhere (T-201-01). Internal columns (object_type_id,
created_at, ...) can never leak because they are never referenced as output
keys.

Stack rule: no app.db, no SQLAlchemy, no FastAPI in this module — only
stdlib plus the two mapping constants imported from the connector-free
app.public_api.jira_shape (DECOUPLE-01).

Native identifiers (DECOUPLE-02, D7): ``objectKey`` resolves Object.key →
stored Key attribute value → object UUID (never null — the Jira object id is
never used), and reference ``searchValue`` follows the same resolved key (its
terminal fallback is the target name). ``objectTypeAttributeId`` is the OTA
UUID in nested and outer attribute forms, symmetric with the public write
path's UUID lookup; the foreign Jira ``jira_type_attribute_id`` is no longer
part of the read wire contract.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.public_api.jira_shape import (
    INDRAMESH_TO_JIRA_TYPE,
    status_category_to_jira,
)

logger = logging.getLogger(__name__)

# Locale-safe month abbreviations (never strftime %b — C-locale dependent).
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Jira defaultType names by defaultTypeId (INDRAMESH_TO_JIRA_TYPE second tuple
# element). Keys are the Jira type-0 default indices, not the top-level type.
_DEFAULT_TYPE_NAMES = {
    0: "Text",
    1: "Integer",
    2: "Boolean",
    3: "Float/Double",  # live-verified (jira_shape comment) — do NOT repurpose to "Group"
    # 4 was wrongly "Version" (Phase 202-01) — real Jira defaultTypeId 4 is
    # DATE; "version" moved to its own top-level type 5 with no defaultTypeId,
    # see jira_shape's 2026-09-14 correction note. No IndraMesh type maps
    # to a bare Date defaultType, so 4 is intentionally absent here.
    6: "DateTime",
    7: "URL",
    8: "Email",
    9: "Textarea",   # 277/D-12: textarea defaultTypeId 9 (paired with the
                    # INDRAMESH_TO_JIRA_TYPE "textarea" entry — extended together)
    10: "Select",
}


@dataclass
class ObjectReadData:
    """Intermediate assembled-read contract for build_object_shape.

    Plain dicts only — constructed by the Phase 201-02 assembly service from
    ORM rows, consumed here without any DB access.
    """

    obj: dict  # {id, name, key, created_at, updated_at}
    object_type: dict  # {id, name, sort, parent_id, schema_id, created_at, updated_at, icon: {url} | None}
    attribute_defs: list[dict] = field(default_factory=list)  # per def: {id, name, description, sort,
    #  attribute_type_id, minimum_cardinality, maximum_cardinality,
    #  is_required, is_hidden, is_unique, structure, referenced_object_type_id, reference_type_id,
    #  data_type: str, reference_type: {id, reference, color} | None, is_label_attribute: bool}
    values: list[dict] = field(default_factory=list)  # {id, object_type_attribute_id, attribute_name,
    #  value_integer, value_float, value_boolean, value_simple, value_datetime, value_struct}
    refs: list[dict] = field(default_factory=list)  # {id, from_attribute_name, to_object_id}
    key_values: dict[str, str] = field(default_factory=dict)  # object_uuid -> stored "Key" attribute
    #  scalar for keyless objects (batched by the caller; Object.key still wins)
    statuses: dict[str, dict] = field(default_factory=dict)  # status_uuid -> {id, name, status_category}
    ref_objects: dict[str, dict] = field(default_factory=dict)  # object_uuid -> {id, name,
    #  key, created_at, updated_at, object_type_id}
    ref_object_types: dict[str, dict] = field(default_factory=dict)  # object_type_uuid -> same shape as object_type
    object_labels: list[dict] = field(default_factory=list)  # the object's own classification
    #  label refs (label_id/key/family/name/position), family sort then label
    #  position/key order; empty for an unclassified object (CLASS-05, D-06)


def format_jira_datetime(dt: datetime) -> str:
    """Jira displayValue datetime format, e.g. ``28/Jul/26 10:47 AM``."""
    return (
        f"{dt.day:02d}/{_MONTH_ABBR[dt.month - 1]}/{dt.year % 100:02d} "
        f"{dt.hour % 12 or 12}:{dt.minute:02d} "
        f"{'AM' if dt.hour < 12 else 'PM'}"
    )


def _epoch_ms(dt: datetime) -> int:
    """Epoch milliseconds for a naive UTC datetime column value."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _native_key_material(node: dict, data: ObjectReadData) -> str | None:
    """Stored native key material for one object contract dict (DECOUPLE-02).

    Priority: ``Object.key`` first, then the object's stored "Key" attribute
    scalar (``data.key_values``). Returns None when neither exists — callers
    append their own terminal fallback. The Jira object id is deliberately
    NOT consulted (no foreign-key fallback).
    """
    stored = node.get("key")
    if stored:
        return str(stored)
    value = data.key_values.get(str(node["id"]))
    return value if value else None


def _resolved_native_key(node: dict, data: ObjectReadData) -> str:
    """Native key chain with the UUID terminal — never null (DECOUPLE-02)."""
    return _native_key_material(node, data) or str(node["id"])


def build_avatar(object_id: str, icon_url: str | None) -> dict:
    """Jira avatar object — all five size keys carry the single local icon URL.

    Sizes are not distinguishable locally (IndraMesh Icon has one url column) —
    documented deviation; objectId is the object UUID-as-string.
    """
    return {
        "url16": icon_url,
        "url48": icon_url,
        "url72": icon_url,
        "url144": icon_url,
        "url288": icon_url,
        "objectId": object_id,
    }


def build_object_type_shape(ot: dict) -> dict:
    """Map an ObjectType contract dict to the golden objectType shape."""
    return {
        "id": str(ot["id"]),
        "name": ot["name"],
        "type": 0,  # Jira int — never a string
        "icon": (
            {
                "id": str(ot["icon"]["id"]),
                "name": ot["icon"]["name"],
                "url16": ot["icon"]["url"],
                "url48": ot["icon"]["url"],
            }
            if ot.get("icon")
            else None
        ),
        "position": ot.get("sort", 0),
        "created": ot["created_at"].isoformat(),
        "updated": ot["updated_at"].isoformat(),
        "objectCount": ot.get("object_count", 0),  # real count supplied by the Phase 202 loader; 0 fallback for legacy callers
        "parentObjectTypeId": str(ot["parent_id"]) if ot.get("parent_id") else None,
        "objectSchemaId": str(ot["schema_id"]),
        "inherited": False,
        "abstractObjectType": ot.get("is_abstract", False),
        "parentObjectTypeInherited": False,
    }


def select_options_csv_from_def(defn: dict) -> str | None:
    """Build Jira's comma-separated Select options string from the def contract.

    Local tolerant implementation (no ORM ObjectTypeAttribute model needed):
    a plain List[str] options structure (documented in-the-wild shape from
    atomic_create_schema) joins directly; dict options are sorted by position
    and read via .get("value") (never indexed, so a dict without "value" is
    skipped instead of raising). The str-structure branch (structure itself
    is a plain string) is preserved as-is.
    """
    structure = defn.get("structure")
    if isinstance(structure, str):
        return structure
    if not isinstance(structure, dict):
        return None
    options = structure.get("options")
    if not options:
        return None
    if all(isinstance(o, str) for o in options):
        values = [o for o in options if o]
        return ",".join(values) if values else None
    sorted_opts = sorted(
        options, key=lambda o: o.get("position", 0) if isinstance(o, dict) else 0
    )
    values = [o["value"] for o in sorted_opts if isinstance(o, dict) and o.get("value")]
    return ",".join(values) if values else None


def _classification_shape(labels: list[dict] | None) -> dict:
    """The camelCase public classification block (CLASS-02/CLASS-04, D-07).

    Input is the internal label-ref list (``label_id``/``key``/``family``/
    ``name``/``position``) in family ``sort``, label ``position``/``key`` order.
    The public wire carries ONLY camelCase keys: ``classificationLabels`` (the
    full ordered list, every element shaped ``{labelId, key, family, name,
    position}``), plus the three convenience views — ``confidentiality`` (the
    single-element family's element or null), ``personalData`` (bool: any
    personal-data label) and ``exportControl`` (the multi-valued family's
    elements). An unclassified target emits the empty defaults. Labels are
    READ-ONLY here: the public write contract never accepts classification
    (D-17).
    """
    entries = [
        {
            "labelId": str(label["label_id"]),
            "key": label["key"],
            "family": label["family"],
            "name": label["name"],
            "position": label["position"],
        }
        for label in labels or []
    ]
    return {
        "classificationLabels": entries,
        "confidentiality": next(
            (e for e in entries if e["family"] == "confidentiality"), None
        ),
        "personalData": any(e["family"] == "personal_data" for e in entries),
        "exportControl": [e for e in entries if e["family"] == "export_control"],
    }


def build_attribute_definition(defn: dict, ref_object_types: dict) -> dict:
    """Map one attribute_def contract dict to the golden objectTypeAttribute shape."""
    data_type = defn["data_type"]
    if data_type == "reference":
        # "reference" IS present in INDRAMESH_TO_JIRA_TYPE (added for HIER-06
        # coverage) but is intentionally NOT read here — falling through to
        # INDRAMESH_TO_JIRA_TYPE.get(...) would still be wrong for reference
        # attrs (needs referenceType/referenceObjectTypeId, which a bare
        # (type_idx, defaultTypeId) tuple can't express). Force the correct
        # index here instead.
        type_idx, default_type_idx = 1, None
    else:
        type_idx, default_type_idx = INDRAMESH_TO_JIRA_TYPE.get(data_type, (0, 0))
    out = {
        "id": str(defn["id"]),
        "name": defn["name"],
        "label": bool(defn.get("is_label_attribute", False)),  # resolved from ObjectType.label_attribute_id by the caller; Phase 201 callers without the key keep false
        "type": type_idx,                              # int — INDRAMESH_TO_JIRA_TYPE[0]
        "editable": not bool(defn.get("is_required")),  # is_required=True -> system-ish non-editable (best-effort)
        "system": False,
        "sortable": True,
        "summable": False,
        "indexed": True,
        "minimumCardinality": defn.get("minimum_cardinality", 0),
        "maximumCardinality": defn.get("maximum_cardinality", -1),
        "removable": not bool(defn.get("is_required")),
        "hidden": bool(defn.get("is_hidden")),
        "includeChildObjectTypes": False,
        "uniqueAttribute": bool(defn.get("is_unique")),
        "options": select_options_csv_from_def(defn) or "",
        "position": defn.get("sort", 0),
    }
    if defn.get("description"):
        out["description"] = defn["description"]
    if type_idx == 0 and default_type_idx is not None:
        out["defaultType"] = {"id": default_type_idx, "name": _DEFAULT_TYPE_NAMES.get(default_type_idx, "Text")}
    if data_type == "reference":
        ref_type = defn.get("reference_type")
        out["referenceType"] = (
            {
                "id": str(ref_type["id"]),
                "name": ref_type["reference"],
                "description": ref_type.get("reference", ""),
                "color": ref_type.get("color", ""),
                "url16": ref_type.get("url16"),
                "removable": False,
                "objectSchemaId": ref_type.get("objectSchemaId"),
            }
            if ref_type
            else None
        )
        ref_ot_id = defn.get("referenced_object_type_id")
        out["referenceObjectTypeId"] = str(ref_ot_id) if ref_ot_id else None
        ref_ot = ref_object_types.get(str(ref_ot_id)) if ref_ot_id else None
        out["referenceObjectType"] = build_object_type_shape(ref_ot) if ref_ot else None
    # CLASS-02/D-07: the camelCase classification block — read-only on the
    # public shape; an unclassified attribute emits the empty defaults.
    out.update(_classification_shape(defn.get("classification_labels")))
    # DECOUPLE-02/D7: the wire id is the resolved OTA UUID string — symmetric
    # with the public write path's UUID lookup — NEVER the foreign Jira
    # jira_type_attribute_id (documented breaking change). The definition id
    # is always present (CTE emits ota.id).
    out["objectTypeAttributeId"] = str(defn["id"])
    return out


def _build_value_shape(v: dict, defn: dict, data: ObjectReadData) -> dict | None:
    """Map one ObjectValue contract dict to a golden objectAttributeValues entry.

    Column mapping: status lives in value_struct, NEVER value_simple. Returns
    None for unresolvable status values — skipped, matching Jira's
    leave-missing behavior.
    """
    data_type = defn["data_type"]
    if data_type == "status":
        struct = v.get("value_struct") or {}
        status_id = struct.get("id")
        status = data.statuses.get(str(status_id)) if status_id else None
        if not status:
            return None
        return {
            "status": {
                "id": str(status["id"]),
                "name": status["name"],
                "category": status_category_to_jira(status["status_category"]),
            },
            "searchValue": status["name"],
            "referencedType": False,
            "displayValue": status["name"],
        }
    if data_type == "integer":
        value = v.get("value_integer")
    elif data_type == "double":
        value = v.get("value_float")
    elif data_type == "boolean":
        value = v.get("value_boolean")
    elif data_type == "datetime":
        dt = v.get("value_datetime")
        if dt is None:
            return None
        value = dt.isoformat()
        return {
            "value": value,
            "searchValue": value,
            "referencedType": False,
            "displayValue": format_jira_datetime(dt),
        }
    else:
        # text/url/email/select/json/version/group/jira/confluence
        value = v.get("value_simple")
    return {
        "value": value,
        "searchValue": value,
        "referencedType": False,
        "displayValue": value,
    }


def _build_reference_value(ref: dict, data: ObjectReadData) -> dict | None:
    """Map one ObjectRef contract dict to a golden reference value shape."""
    target = data.ref_objects.get(str(ref["to_object_id"]))
    if not target:
        return None
    target_type = data.ref_object_types.get(str(target["object_type_id"])) or data.object_type
    icon = target_type.get("icon")
    target_key = _resolved_native_key(target, data)
    return {
        "referencedObject": {
            "id": str(target["id"]),
            "label": target["name"],
            "objectKey": target_key,
            "avatar": build_avatar(str(target["id"]), icon.get("url") if icon else None),
            "objectType": build_object_type_shape(target_type),
            "created": target["created_at"].isoformat(),
            "updated": target["updated_at"].isoformat(),
            "hasAvatar": False,
            "timestamp": _epoch_ms(target["updated_at"]),
            "_links": {"self": f"/public/api/v1/object/{target['id']}"},
            "archived": False,
            "name": target["name"],
        },
        "searchValue": target_key or target["name"],
        "referencedType": True,
        "displayValue": target["name"],
    }


def build_attribute_values(data: ObjectReadData) -> list[dict]:
    """Assemble the golden attributes[] array from ObjectReadData.

    ObjectValue scalars (incl. status) and ObjectRef references are merged,
    grouped by attribute definition, emitted in definition order.
    """
    defn_by_id = {str(d["id"]): d for d in data.attribute_defs}
    defn_by_name_lower = {d["name"].lower(): d for d in data.attribute_defs}
    entries: dict[str, dict] = {}  # def id -> attribute entry

    # --- scalar + status values (ObjectValue rows) ---
    for v in data.values:
        defn = defn_by_id.get(str(v["object_type_attribute_id"]))
        if not defn:
            # WR-01: object_value_service.py permits writing a value against
            # an ANCESTOR type's OTA. defn_by_id only holds the DISTINCT ON
            # (name) inheritance winners, so a value written against an OTA
            # that's since been shadowed by a child type's own OTA of the
            # same name misses here even though the def resolves fine by
            # name. Fall back to the name lookup instead of dropping the
            # value silently.
            attr_name = (v.get("attribute_name") or "").lower()
            defn = defn_by_name_lower.get(attr_name) if attr_name else None
            if defn is None:
                logger.warning(
                    "public object read: value %s (object_type_attribute_id=%s, name=%r) "
                    "matches no attribute definition by id or name — dropped from response",
                    v.get("id"), v.get("object_type_attribute_id"), v.get("attribute_name"),
                )
        if not defn or defn["data_type"] == "reference":
            continue  # references live on ObjectRef, never ObjectValue
        shape = _build_value_shape(v, defn, data)
        if shape is None:
            continue  # unresolvable status — leave missing (Jira behavior)
        attr_def = build_attribute_definition(defn, data.ref_object_types)
        entry = entries.setdefault(str(defn["id"]), {
            "id": str(v["id"]),
            "objectTypeAttribute": attr_def,
            "objectTypeAttributeId": attr_def["objectTypeAttributeId"],
            "objectAttributeValues": [],
            "objectId": str(data.obj["id"]),
        })
        entry["objectAttributeValues"].append(shape)

    # --- reference values (ObjectRef rows) ---
    refs_by_name: dict[str, list[dict]] = {}
    for ref in data.refs:
        name = (ref.get("from_attribute_name") or "").lower()
        refs_by_name.setdefault(name, []).append(ref)
    for name_lower, refs in refs_by_name.items():
        defn = defn_by_name_lower.get(name_lower)
        if not defn or defn["data_type"] != "reference":
            continue
        attr_def = build_attribute_definition(defn, data.ref_object_types)
        entry = entries.setdefault(str(defn["id"]), {
            "id": str(refs[0]["id"]),
            "objectTypeAttribute": attr_def,
            "objectTypeAttributeId": attr_def["objectTypeAttributeId"],
            "objectAttributeValues": [],
            "objectId": str(data.obj["id"]),
        })
        for ref in refs:
            shape = _build_reference_value(ref, data)
            if shape is not None:
                entry["objectAttributeValues"].append(shape)

    # definition order, not value order
    ordered = []
    for d in data.attribute_defs:
        if str(d["id"]) in entries:
            ordered.append(entries[str(d["id"])])
    return ordered


def build_object_shape(data: ObjectReadData) -> dict:
    """Assemble the top-level Jira Object shape from ObjectReadData."""
    icon = data.object_type.get("icon")
    out = {
        "id": str(data.obj["id"]),
        "label": data.obj["name"],
        # DECOUPLE-02: Object.key -> Key attribute value -> UUID, never null.
        "objectKey": _resolved_native_key(data.obj, data),
        "avatar": build_avatar(str(data.obj["id"]), icon.get("url") if icon else None),
        "objectType": build_object_type_shape(data.object_type),
        "created": data.obj["created_at"].isoformat(),
        "updated": data.obj["updated_at"].isoformat(),
        "hasAvatar": False,
        "timestamp": _epoch_ms(data.obj["updated_at"]),
        "attributes": build_attribute_values(data),
        "extendedInfo": {"openIssuesExists": False, "attachmentsExists": False},
        "_links": {"self": f"/public/api/v1/object/{data.obj['id']}"},
        "archived": False,
        "name": data.obj["name"],
    }
    # CLASS-05/D-06: the object's own classification labels, camelCase and
    # read-only (D-17: the public write contract never accepts classification).
    out.update(_classification_shape(data.object_labels))
    return out


def has_reference_entries(shape: dict) -> bool:
    """True iff the shape carries at least one reference attribute entry
    (only reference definitions carry the ``referenceObjectType`` key)."""
    return any(
        "referenceObjectType" in (entry.get("objectTypeAttribute") or {})
        for entry in shape.get("attributes") or []
    )


def strip_hidden_references(shape: dict, visible_targets: dict[str, str]) -> dict:
    """Per-caller record filter on a built object shape (quick 261012-blo, B-04).

    ``visible_targets`` maps every referenced object id the caller may read
    to its object type id. A reference value whose target is not in it is
    removed; an entry that lost values and has none left is omitted; a
    ``referenceObjectType`` without a visible target of that type becomes
    ``None`` (``referenceObjectTypeId`` stays). The result equals the shape
    built as if the hidden references did not exist, except for the entry
    ``id`` (first ObjectRef row id). Pure: the input — a possibly shared,
    cached shape — is never mutated; changed parts are rebuilt.
    """
    visible_type_ids = set(visible_targets.values())
    if "attributes" not in shape:
        return dict(shape)
    new_attributes = []
    for entry in shape["attributes"] or []:
        definition = entry.get("objectTypeAttribute") or {}
        if "referenceObjectType" not in definition:
            new_attributes.append(entry)
            continue
        values = entry.get("objectAttributeValues") or []
        kept = [
            v for v in values
            if not isinstance(v.get("referencedObject"), dict)
            or v["referencedObject"].get("id") in visible_targets
        ]
        if len(kept) < len(values) and not kept:
            continue
        ref_type = definition.get("referenceObjectType")
        new_definition = definition
        if isinstance(ref_type, dict) and ref_type.get("id") not in visible_type_ids:
            new_definition = {**definition, "referenceObjectType": None}
        if len(kept) == len(values) and new_definition is definition:
            new_attributes.append(entry)
            continue
        new_attributes.append({
            **entry,
            "objectTypeAttribute": new_definition,
            "objectAttributeValues": kept,
        })
    return {**shape, "attributes": new_attributes}


def strip_denied_attributes(shape: dict, visible_ota_ids: set[str]) -> dict:
    """Per-caller attribute-type DENY strip on a built object shape (D-11).

    ``visible_ota_ids`` holds the ``objectTypeAttributeId`` of every entry whose
    attribute type the caller may read. An entry outside it is dropped whole —
    its name (``objectTypeAttribute``) and its values
    (``objectAttributeValues``) together — so **a denied attribute type is
    indistinguishable from an attribute that does not exist**: no ``null``
    placeholder, no empty value list, no ``hidden``/``denied`` marker. An entry
    without an ``objectTypeAttributeId`` cannot be vouched for and is dropped.

    Pure: the input — a shared, caller-independent cached shape — is never
    mutated; the surviving entries keep their original order (the list is
    rebuilt, never re-sorted).
    """
    if "attributes" not in shape:
        return dict(shape)
    kept = [
        entry
        for entry in (shape["attributes"] or [])
        if str(entry.get("objectTypeAttributeId")) in visible_ota_ids
    ]
    return {**shape, "attributes": kept}
