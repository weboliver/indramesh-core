"""Connector-free Jira wire shape for the public read API (DECOUPLE-01).

Single source of truth for the Jira-compatible status category values the
public API emits. Intentionally dependency-free: no connector, DB, SQLAlchemy
or FastAPI imports, so public read paths never load connector code.
"""

# Jira Assets status-type "category" wire values, confirmed live (2026-07-27)
# by cross-referencing real status names against their category codes (e.g.
# "Active"/"Running"/"Licensed"=1, "Closed"/"Stopped"/"Decommissioned"=0,
# "In Service"/"Change In Progress"/"Reserved"=2). The keys are the category
# NAMES; the int values are the Jira wire category codes, distinct from
# StatusCategory's own string values.
STATUS_CATEGORY_TO_JIRA = {"Inactive": 0, "Active": 1, "Pending": 2}


def status_category_to_jira(category) -> int:
    key = category.value if hasattr(category, "value") else category
    return STATUS_CATEGORY_TO_JIRA.get(key, 1)


# IndraMesh → Jira attribute type index mapping.
#
# Verified live against a real Jira 11.3.6 Assets instance (2026-07-27, see
# 172-03-SUMMARY.md follow-up): the top-level "type" field is 0 ("Default")
# for almost every simple attribute kind, and the REAL sub-kind is carried in
# "defaultTypeId" (0=Text, 1=Integer, 3=Float/Double, 6=DateTime, 7=URL,
# 8=Email, 10=Select — confirmed by probing a live object type's existing
# attributes AND by test-creating one of each against real Jira). Status (7),
# Reference (1), User (2), and Group (4) are genuinely independent TOP-LEVEL
# types with no defaultTypeId of their own.
#
# "group" was wrongly classified here as defaultTypeId=3 under the Default
# container (type=0) — colliding with "double"'s real mapping — when Group is
# actually its own independent top-level type (4). Verified 2026-09-13 by
# disassembling the deployed Jira 11.3.6 / Insight 21.3.6 Assets plugin's own
# `ObjectTypeAttributeBean$Type` enum via `javap` against
# `META-INF/lib/insight-core-model-21.3.6-REL-0001.jar` nested inside
# `insight-21.3.6-REL-0001.jar` (read-only, no Jira REST calls). The identical
# disassembly also showed "version": (0, 4) a few lines below had the exact
# same bug class — defaultTypeId=4 under type=0 is actually DATE, and VERSION
# is its own independent top-level type=5. Fixed 2026-09-14 (same bytecode
# evidence, no new live re-verification needed): "version" is now (5, None).
#
# Boolean was WRONGLY classified here as an independent top-level type
# (type=2) in an earlier fix — confirmed live (2026-07-27) this was itself
# wrong: type=2 is actually Jira's top-level index for **User**-typed
# attributes (verified against real "User Access"/"Support"/"Manager"/"JIRA
# User" attributes, all type=2). Every Boolean attribute exported under the
# old mapping was therefore silently created as a User picker field instead
# ("Beim Export wird Boolean zum User Objekt"). The correct mapping,
# confirmed against real live Boolean attributes ("DHCP",
# "Active"), is type=0 ("Default") with defaultTypeId=2 — same pattern as
# every other simple type.
#
# The previous version of this table wrongly sent the sub-kind id as BOTH
# "type" and "defaultTypeId" — e.g. Select was sent as {"type": 9, ...},
# but 9 is Textarea's defaultTypeId, not a valid top-level type, so every
# pushed Select attribute silently landed in Jira as a Textarea field.
# Worse, Integer was sent as {"type": 1, ...} — but 1 is Jira's real
# top-level index for Reference/Object attributes, so Jira interpreted the
# push as "create a reference attribute" and rejected it for missing
# reference-specific fields ("Object Type invalid, ReferenceType invalid").
#
# "reference" is present in this table for HIER-06 type-coverage completeness
# (public read API), but build_attribute_definition (object_shape.py) does
# NOT consult it for reference attrs — it keeps its own hardcoded special-case
# because a bare (type_idx, defaultTypeId) tuple can't express the extra
# referenceType/referenceObjectTypeId fields a reference attribute needs.
INDRAMESH_TO_JIRA_TYPE: dict[str, tuple[int, int | None]] = {
    "text": (0, 0),        # Default Text
    "integer": (0, 1),     # Default Integer
    "boolean": (0, 2),     # Default Boolean — NOT top-level type=2, that's User
    "double": (0, 3),      # Default Float/Double
    "datetime": (0, 6),    # Default DateTime
    "url": (0, 7),         # Default URL
    "email": (0, 8),       # Default Email
    "select": (0, 10),     # Default Select
    "status": (7, None),   # Status — own top-level type, no defaultTypeId
    "reference": (1, None),          # HIER-06: top-level type 1, no defaultTypeId
    "json": (0, 0),        # no Jira native — Text fallback (documented deviation)
    "version": (5, None),  # Version — own top-level type, no defaultTypeId; see correction note above (2026-09-14)
    "group": (4, None),    # Group — own top-level type, no defaultTypeId; see correction note above (2026-09-13)
    "user": (2, None),     # User — own top-level type, no defaultTypeId
    "jira": (0, 0),        # no Jira native — Text fallback (documented deviation)
    "confluence": (0, 0),  # no Jira native — Text fallback (documented deviation)
    # 277/D-08: textarea is Jira's Default container (type 0) with
    # defaultTypeId 9 — 9 is Textarea's sub-type, never a top-level type
    # (the same live-verified contract as the comment above). This entry
    # feeds the public Jira-compatible shape (D-09/D-12).
    "textarea": (0, 9),
    # Pull artifact with 66 DB rows — NOT in AttributeTypeDataType (do not add
    # to the enum); explicit (0,0) so reads never KeyError (T-202-06b).
    "custom_unknown_type": (0, 0),
}
# Each value: (type_index, defaultTypeId_or_None) — None means omit
# defaultTypeId from the create payload entirely.