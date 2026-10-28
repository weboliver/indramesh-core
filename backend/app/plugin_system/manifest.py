"""Plugin manifest contract (D-01) — plugin.toml parsed with stdlib tomllib.

The manifest is DATA, not code: metadata, declared capabilities, config schema
and frontend/database references all live in ``plugin.toml``. Parsing is
read-only by design (stdlib ``tomllib`` has no writer) — the core never writes
a plugin's manifest back.
"""

import re
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Mirrors loader.py's `_PLUGIN_NAME_RE` (T-282-04 path-traversal guard): the
# manifest's own `name` becomes the DB key, Redis key, logger namespace and
# route prefix (WR-07), so it must be constrained the same way as the
# directory-entry name, not just validated as "a dict" / "a str".
_MANIFEST_NAME_PATTERN = r"^[a-zA-Z0-9_-]+$"

# Phase 310 (PUB-06/D-08): a manifest may declare the plugin's license as an SPDX
# identifier or expression (`Apache-2.0`, `AGPL-3.0-or-later`, `MIT OR Apache-2.0`).
# The value is pure metadata — the core reads no behaviour from it and never grants
# anything from it; it exists so a published plugin tree states its license next to
# its LICENSE file. The pattern is therefore a shape guard in the same allow-list
# style as `_MANIFEST_NAME_PATTERN` (SPDX id characters, with `AND`/`OR`/`WITH`
# joining id tokens for an expression), not a full SPDX grammar validator: a value
# like `MIT OR Apache-2.0` must pass while a bare license *name* (`Apache 2.0`), a
# path or an empty string is a mistake worth failing the load on.
_SPDX_LICENSE_PATTERN = r"^[A-Za-z0-9.+()-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+()-]+)*$"

# Phase 287 (PLUGDB-01, T-287-02): the ONLY accepted shape of a plugin's
# private Alembic version table. The core's own `alembic_version` is rejected
# by the pattern itself (the trailing `_<segment>` is required), which is
# deliberate — the runner must never mistake the core table for a plugin's
# (RESEARCH Pitfall 9). `version_table` is later interpolated as a SQL
# identifier (identifiers cannot be bind-parameterised), so this regex is the
# one true injection control for it.
VERSION_TABLE_PATTERN = r"^alembic_version_[a-z0-9_]+$"
_VERSION_TABLE_RE = re.compile(VERSION_TABLE_PATTERN)

# PostgreSQL identifiers are limited to NAMEDATALEN-1 = 63 bytes; a longer
# identifier is silently truncated by the server, which would desynchronise
# the manifest's declared name from the table actually read.
VERSION_TABLE_MAX_LENGTH = 63
MIGRATIONS_DIR_MAX_LENGTH = 255

# Phase 303 (SEAM-06/D-14): a deferred-asset map declares logical asset ids that
# resolve to plugin-relative heavy payloads. An id is a logical, safe identifier
# (not a path); a path reuses the traversal-free relative-path rule below. The
# asset route further clamps every resolved path to the manifest bundle
# directory.
DEFERRED_ASSET_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"
_DEFERRED_ASSET_ID_RE = re.compile(DEFERRED_ASSET_ID_PATTERN)
DEFERRED_ASSET_PATH_MAX_LENGTH = 255

# Phase 287 (PLUGDB-02, A-287-02): the plugin table namespace. A manifest name
# such as `My-Plugin` is normalised (lowercase + `-` -> `_`, because Postgres
# unquoted identifiers cannot contain `-`) to `my_plugin`, so its tables live
# under `plugin_my_plugin_`.
_NORMALIZED_NAME_RE = re.compile(r"[^a-z0-9_]")


def normalize_plugin_name_for_identifier(name: str) -> str:
    """Normalise a manifest name into a Postgres-safe identifier segment."""
    return _NORMALIZED_NAME_RE.sub("_", name.lower().replace("-", "_"))


def plugin_table_prefix(name: str) -> str:
    """Return the ``plugin_<normalized-name>_`` table prefix for ``name``."""
    return f"plugin_{normalize_plugin_name_for_identifier(name)}_"


def _validate_version_table(value: str) -> str:
    if len(value) > VERSION_TABLE_MAX_LENGTH or not _VERSION_TABLE_RE.match(value):
        raise ValueError(
            f"version_table must match {VERSION_TABLE_PATTERN} and be at most "
            f"{VERSION_TABLE_MAX_LENGTH} characters (got {value!r})"
        )
    return value


def _validate_migrations_dir(value: str) -> str:
    """Reject anything but a relative, traversal-free directory path (T-287-01)."""
    if not value:
        raise ValueError("migrations_dir must be non-empty")
    if len(value) > MIGRATIONS_DIR_MAX_LENGTH:
        raise ValueError(
            f"migrations_dir must be at most {MIGRATIONS_DIR_MAX_LENGTH} characters"
        )
    if "\\" in value:
        raise ValueError("migrations_dir must not contain a backslash")
    if value.startswith("/"):
        raise ValueError("migrations_dir must be a relative path, not absolute")
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(
            "migrations_dir must not contain empty, '.' or '..' path segments"
        )
    return value


def _validate_relative_asset_path(value: str) -> str:
    """Reject anything but a relative, traversal-free asset path (T-303-23).

    Same segment rule as ``_validate_migrations_dir``: no empty/``.``/``..``
    segments, no backslash and no absolute path. The asset route additionally
    clamps the resolved file to the manifest bundle directory, so this parse-time
    check is defence in depth, not the only containment control.
    """
    if not value:
        raise ValueError("deferred asset path must be non-empty")
    if len(value) > DEFERRED_ASSET_PATH_MAX_LENGTH:
        raise ValueError(
            "deferred asset path must be at most "
            f"{DEFERRED_ASSET_PATH_MAX_LENGTH} characters"
        )
    if "\\" in value:
        raise ValueError("deferred asset path must not contain a backslash")
    if value.startswith("/"):
        raise ValueError("deferred asset path must be relative, not absolute")
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(
            "deferred asset path must not contain empty, '.' or '..' path segments"
        )
    return value


# Phase 303 (SEAM-01/D-02): the exact system-role vocabulary a manifest may
# name in ``default_roles``. Keep in sync with init_service's seeded roles —
# an unknown string must fail validation rather than silently create a role.
CORE_ROLE_NAMES = frozenset({"Administrator", "Editor", "Viewer"})

# Permission.resource/action are String(100) columns; bound the manifest
# segments to the same width so a too-long declaration fails at parse time
# instead of at the first flush.
PERMISSION_SEGMENT_MAX_LENGTH = 100
PERMISSION_DESCRIPTION_MAX_LENGTH = 500


class PluginPermissionDeclaration(BaseModel):
    """One manifest-declared permission/action (SEAM-01, D-01..D-04).

    The stable permission name is derived as ``f"{resource}:{action}"`` — there
    is deliberately no independent ``name`` field and no default-class field:
    standard roles are named explicitly per action (D-02). All fields are
    strict (``extra="forbid"``) so a typo cannot smuggle an unvalidated key.
    """

    model_config = ConfigDict(extra="forbid")

    resource: str
    action: str
    description: str | None = None
    default_roles: list[str] = []

    @field_validator("resource", "action")
    @classmethod
    def validate_segment(cls, value: str) -> str:
        if not value:
            raise ValueError("permission resource/action must be non-empty")
        if ":" in value:
            raise ValueError("permission resource/action must not contain ':'")
        if len(value) > PERMISSION_SEGMENT_MAX_LENGTH:
            raise ValueError(
                f"permission resource/action must be at most "
                f"{PERMISSION_SEGMENT_MAX_LENGTH} characters"
            )
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > PERMISSION_DESCRIPTION_MAX_LENGTH:
            raise ValueError(
                "permission description must be at most "
                f"{PERMISSION_DESCRIPTION_MAX_LENGTH} characters"
            )
        return value

    @field_validator("default_roles")
    @classmethod
    def validate_default_roles(cls, value: list[str]) -> list[str]:
        unknown = [role for role in value if role not in CORE_ROLE_NAMES]
        if unknown:
            raise ValueError(f"unknown default role(s): {unknown}")
        return value

    @property
    def name(self) -> str:
        """The stable ``resource:action`` identity for this declaration."""
        return f"{self.resource}:{self.action}"


class PluginFrontendSpec(BaseModel):
    """Declared frontend bundle for a plugin (consumed by Phase 284).

    Phase 303 (SEAM-06/D-14): ``deferred_assets`` maps a logical asset id to a
    plugin-relative heavy-payload path that is fetched only on use. It is kept
    separate from the lightweight registration ``bundle`` so the registration
    descriptor never carries editor bytes, third-party CSS text or worker bytes.
    """

    model_config = ConfigDict(extra="forbid")

    bundle: str
    extension_points: list[str] = []
    deferred_assets: dict[str, str] = {}

    @field_validator("deferred_assets")
    @classmethod
    def validate_deferred_assets(cls, value: dict[str, str]) -> dict[str, str]:
        for asset_id, asset_path in value.items():
            if not _DEFERRED_ASSET_ID_RE.match(asset_id):
                raise ValueError(
                    f"deferred asset id must match {DEFERRED_ASSET_ID_PATTERN} "
                    f"(got {asset_id!r})"
                )
            _validate_relative_asset_path(asset_path)
        return value


class PluginDatabaseSpec(BaseModel):
    """Declared plugin-owned database migration target (PLUGDB-01).

    Phase 287: a database-owning plugin declares its private migrations
    directory and version table here. The manifest still *declares*
    ``version_table``, but the core accepts only the one derived from the
    plugin's own name (see ``PluginManifest``'s model validator) so a plugin
    cannot claim another plugin's version table by declaring it (review
    2026-09-22, load-order hijack).
    """

    model_config = ConfigDict(extra="forbid")

    migrations_dir: str
    version_table: str
    # Phase 292 (JDATA-05): the manifest-flag admission contract. A plugin
    # declaring this flag may only be migrated by a verified restricted login
    # role (a non-superuser, non-CREATEDB/CREATEROLE/BYPASSRLS role that can log
    # in). ``run_plugin_migrations`` refuses it per-plugin BEFORE object adoption
    # or any revision when the role is absent or elevated, so a plugin that needs
    # the boundary never silently migrates through the privileged fallback.
    # Defaults False so every existing plugin keeps the legacy behaviour.
    require_restricted_role: bool = False

    @field_validator("version_table")
    @classmethod
    def validate_version_table(cls, v: str) -> str:
        return _validate_version_table(v)

    @field_validator("migrations_dir")
    @classmethod
    def validate_migrations_dir(cls, v: str) -> str:
        return _validate_migrations_dir(v)


class PluginManifest(BaseModel):
    """Validated shape of a plugin's ``plugin.toml``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_MANIFEST_NAME_PATTERN)
    version: str
    author: str
    # Phase 310 (PUB-06/D-08): optional SPDX identifier/expression. Declared by all
    # six shipped manifests, but optional so a third-party plugin written before
    # this key existed keeps loading — an absent value means "not declared", never
    # "no license". Metadata only: nothing in the core branches on it.
    license: str | None = Field(default=None, pattern=_SPDX_LICENSE_PATTERN)
    api_version: str
    capabilities: list[str]
    config_schema: dict[str, Any] = {}
    frontend: PluginFrontendSpec | None = None
    database: PluginDatabaseSpec | None = None
    # Phase 303 (SEAM-01/Carry-01): plugin-owned permission declarations. Each
    # entry is registered at discovery as a persisted Core permission owned by
    # this plugin; its stable name is ``resource:action``.
    permissions: list[PluginPermissionDeclaration] = []
    # PLUGWORKER-01: additive opt-in flag, defaults False for every existing
    # plugin. A plugin relying on worker-side behavior sets it so the future
    # admin badge (Phase 285) can flag a worker that still needs a reload.
    requires_worker_update: bool = False
    # Phase 304 (GQL-06/D-16/D-18): additive opt-in flag, defaults False for
    # every existing plugin. A plugin that sets it declares that activating it
    # unlocks the HTTP GraphQL endpoint (``/api/v1/graphql``) in production;
    # outside production the endpoint is always served. Core reads only this
    # boolean and never names a plugin (D-18). Trust model: plugins are
    # administrator-installed code, so the flag is trusted exactly like every
    # other manifest field — any plugin declaring it may enable the endpoint in
    # production (accepted). The Core admin surface may show a "not intended for
    # production" warning (D-18) derived from this flag.
    enables_http_graphql: bool = False
    # Only consulted on first discovery (no plugins row yet): False persists the
    # plugin DISABLED until an operator activates it, e.g. a demo plugin that
    # must not run in a real installation. An existing row always wins.
    active_by_default: bool = True
    # Phase 318 (host-B clean-host finding): declares that this plugin is only
    # useful when an optional infrastructure feature is present — today the log
    # store the bundled log stack provides. Metadata only: the Core never
    # branches on it and never names a plugin, and a missing feature is not an
    # error (the plugin reports ``not_configured`` through its own status route).
    # The plugin installers read the flag to print the operator's remedy, because
    # an installed-yet-unusable plugin is otherwise indistinguishable from a
    # broken one on a fresh default quickstart.
    requires_log_store: bool = False
    # Phase 318 (host-B no-LLM finding): declares that this plugin only works
    # with a configured Core LLM client. Without one the plugin hides its AI
    # actions by design, so an active plugin looked healthy yet did nothing. The
    # Plugins admin page projects the Core's own LLM status next to a plugin
    # declaring the flag, and the plugin installers print the remedy. The Core
    # never names a plugin and never branches on the flag otherwise.
    requires_llm: bool = False
    # Only required for directory-scan discovery (D-03) — entry-point discovery
    # (Plan 282-03) resolves the class via the entry point's own dotted path.
    module: str | None = None

    @model_validator(mode="after")
    def validate_database_version_table_is_derived(self) -> "PluginManifest":
        """PLUGDB-01/T-287-02: a plugin may only own the version table its name derives.

        Without this, a plugin declaring ``alembic_version_<other>`` could sort
        before its victim and claim that plugin's version table under
        first-owner-wins. The normalisation-collision case (`my-plugin` vs
        `my_plugin`) stays plan 287-02's claim-registry job.
        """
        if self.database is None:
            return self
        expected = "alembic_version_" + normalize_plugin_name_for_identifier(self.name)
        if self.database.version_table != expected:
            raise ValueError(
                f"database.version_table must be {expected!r} for plugin "
                f"{self.name!r} (got {self.database.version_table!r})"
            )
        return self

    @model_validator(mode="after")
    def validate_unique_permission_declarations(self) -> "PluginManifest":
        """SEAM-01/D-02: no duplicate ``resource:action`` declaration.

        Two entries with the same derived name would make the first-insert
        grant set ambiguous (and the insert-returning contract meaningless), so
        the whole manifest is rejected at parse time.
        """
        names = [declaration.name for declaration in self.permissions]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate permission declarations: {duplicates}")
        return self


def load_manifest_toml(raw: bytes) -> PluginManifest:
    """Parse raw ``plugin.toml`` bytes into a validated ``PluginManifest``.

    Raises ``tomllib.TOMLDecodeError`` on malformed TOML and Pydantic
    ``ValidationError`` on a schema mismatch; callers isolate both as an
    error-state discovery result rather than letting them crash the scan.
    """
    data = tomllib.loads(raw.decode("utf-8"))
    return PluginManifest.model_validate(data)
