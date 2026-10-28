"""CLOSED metadata and pure guard for retired Core request fields (Phase 296).

The Jira carve-out removed a fixed, closed set of top-level request keys from the
Core API. A request that still carries any of them is rejected by *presence* —
independent of the submitted value, including ``null``/``false``/empty — so a
client can never silently smuggle a retired field back in (D-06).

This module is deliberately dependency-light: it imports nothing from the app so
it can be loaded by an offline check and used as a Pydantic before-validator
without triggering the connector registry import cycle.

Contract (D-07/D-08):
- Only the listed top-level keys are rejected; no global extra-policy change and
  no recursion into nested config or ``plugin_parameters``.
- The error names every offending key (sorted) and states the Core/plugin
  boundary, never echoing submitted values.
- Non-mapping inputs pass through unchanged so ORM/response objects are
  unaffected.
"""

from typing import Any, Iterable, Mapping

# Exact model-name -> immutable set of removed top-level keys. This is the
# CLOSED rejection matrix: retired fields live here ONLY, never as accepted
# fields, aliases or functional config. Both the Create and Update verb of each
# family appear, because each declares its own contract.
RETIRED_CORE_INPUTS: dict[str, frozenset[str]] = {
    "SchemaCreate": frozenset({"jira_schema_id", "is_synched"}),
    "SchemaUpdate": frozenset({"jira_schema_id", "is_synched"}),
    "ReferenceTypeCreate": frozenset({"jira_reference_type_id", "is_synched"}),
    "ReferenceTypeUpdate": frozenset({"jira_reference_type_id", "is_synched"}),
    "ObjectTypeCreate": frozenset({"jira_type_id", "is_synched"}),
    "ObjectTypeUpdate": frozenset({"jira_type_id", "is_synched"}),
    "ObjectCreate": frozenset({"jira_object_id", "is_synched"}),
    "ObjectUpdate": frozenset({"jira_object_id", "is_synched"}),
    "ObjectTypeAttributeCreate": frozenset({"jira_type_attribute_id", "is_synched"}),
    "ObjectTypeAttributeUpdate": frozenset({"jira_type_attribute_id", "is_synched"}),
    "StatusCreate": frozenset({"jira_status_id"}),
    "StatusUpdate": frozenset({"jira_status_id"}),
    "TaskCreate": frozenset(
        {"jira_schema_id", "relink_rewire", "include_comments_files"}
    ),
    "TaskUpdate": frozenset({"relink_rewire", "include_comments_files"}),
    "TargetCreate": frozenset({"is_cloud", "type", "default_conflict_strategy"}),
    "TargetUpdate": frozenset({"is_cloud", "type", "default_conflict_strategy"}),
}


def reject_retired_core_inputs(model_names: str | Iterable[str], data: Any) -> Any:
    """Return ``data`` unchanged, or raise ``ValueError`` if a retired key is present.

    Membership on raw input keys is the rejection test — deliberately no truth
    test, prefix test or recursive scan (D-06/D-07). Non-mapping inputs (e.g. an
    ORM instance handed to a from-attributes response model) pass through.
    ``model_names`` may be one name or several (a model's MRO); the retired
    sets of every listed name are united, so a subclass of a guarded model
    keeps its base's rejections.
    """
    if not isinstance(data, Mapping):
        return data
    names = (model_names,) if isinstance(model_names, str) else model_names
    retired = frozenset().union(
        *(RETIRED_CORE_INPUTS.get(name, frozenset()) for name in names)
    )
    if not retired:
        return data
    offending = sorted(retired.intersection(data))
    if offending:
        raise ValueError(
            "Removed Core request field(s): "
            + ", ".join(offending)
            + ". Jira-specific linking/sync management is no longer part of the "
            "Core API; use the Jira plugin where applicable."
        )
    return data
