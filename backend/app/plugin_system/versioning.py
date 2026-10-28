"""api_version compatibility gate (PLUGCORE-08) — SemVer, additive-only.

``CORE_API_VERSION`` is the plugin system's OWN internal contract version: the
version of the plugin API surface (``PluginContext`` methods, hook signatures,
capability interfaces) that a plugin is written against. It is deliberately NOT
tied to the app's Docker image tag or git version — ``1.0.0`` is the
conventional SemVer starting point for the brand-new v19 plugin contract.

Compatibility rule (RESEARCH.md Assumption A1, the standard "additive-only"
reading of CONTEXT.md's SemVer constraint) — a plugin is compatible when it
targets the SAME major version and a minor no newer than the core's:

    core 1.3.0 + plugin 1.2.0  -> compatible   (nothing it relies on was removed)
    core 1.2.0 + plugin 1.3.0  -> INCOMPATIBLE (may call surface that does not exist yet)
    core 2.0.0 + plugin 1.9.0  -> INCOMPATIBLE (major mismatch)

PATCH is deliberately not compared this phase, and pre-release/build metadata
are out of scope — only strict ``MAJOR.MINOR.PATCH`` is accepted ("prefer too
little", CONTEXT.md D-07's philosophy applied to the version grammar).
"""

import re

# The plugin-contract version this core implements (see module docstring).
#
# Why 1.0.0 is the release zero point (317-08, EXT-10-P1): v19-v21 shipped while
# ``docs/STABILITY.md`` still lists the plugin interface under "Excluded
# Surfaces" — experimental, outside the SemVer promise. 1.0.0 is therefore the
# FIRST version the gate actually promises, not a continuation of pre-release
# numbering. An unreported surface change is what this constant cannot catch on
# its own, hence the snapshot in ``backend/tests/test_plugin_manifest.py``.
#
# Per-minor change list — append here, never edit silently. Each minor states
# what it ADDED; a breaking change must raise the major instead.
#   1.0  baseline: plugin base class, capability interfaces (api routes,
#        connector, graph extension, search provider, task type), the
#        immutable declarations, bounded errors, TaskCredentials/TaskStatus,
#        and the authorization dependency factory.
#   1.0  v20 DTO identity change: plugins author and receive Core DTOs rather
#        than hand-built dicts (sdk.py's breaking-contract note); the DTO
#        field sets are part of the pinned surface.
#   1.0  v21 translation interface: record-scope authorization, read/write of
#        stored translations, the entity page, and the UI translation catalog,
#        plus the manifest extensions (permissions, frontend, database,
#        requires_worker_update, enables_http_graphql, active_by_default).
#   1.0  v22 attribute classification: ObjectTypeAttributeCreate/Update gain
#        confidentiality, personal_data and export_control (optional label ids).
#   1.0  v22 object classification: ObjectCreate/Update gain confidentiality,
#        personal_data and export_control (optional label ids).
#   1.0  v22 template classification: SchemaTreeAttribute gains an optional
#        classification block (label keys).
#   1.0  v22 translation run kinds: TranslationEntityPageRequest gains the
#        optional translation_types filter (at most six inventory nouns). Additive
#        and optional, so the pinned surface moves without a version bump; the
#        snapshot in tests/test_plugin_manifest.py was updated with this line.
#   1.0  v22 requirement flags: the manifest gains the optional
#        requires_log_store and requires_llm booleans (default false). Additive
#        and optional, so the pinned surface moves without a version bump.
CORE_API_VERSION = "1.0.0"

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(value: str) -> tuple[int, int, int]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` string into an int tuple.

    Raises ``ValueError`` with the rejected value in the message for anything
    else — including pre-release/build metadata, which this phase does not
    support.
    """
    match = _SEMVER_RE.match(value)
    if match is None:
        raise ValueError(f"Invalid api_version (expected MAJOR.MINOR.PATCH): {value!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_compatible(core_api_version: str, plugin_api_version: str) -> bool:
    """Return whether a plugin targeting ``plugin_api_version`` may run here.

    Additive-only: same MAJOR, and ``plugin_minor <= core_minor``. Raises
    ``ValueError`` if either version string is malformed.
    """
    core_major, core_minor, _ = parse_semver(core_api_version)
    plugin_major, plugin_minor, _ = parse_semver(plugin_api_version)
    return core_major == plugin_major and plugin_minor <= core_minor
