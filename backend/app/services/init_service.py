import logging
import os
import uuid
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.db import async_session_maker
from app.models import (
    User,
    Role,
    UserProfile,
    UserRole,
    SchemaTemplate,
    Permission,
    RolePermission,
    Language,
    Translation,
    TranslationType,
    UiString,
    AttributeType,
    ClassificationFamily,
    ClassificationLabel,
)

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


async def init_default_user() -> tuple[str, str]:
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.username == "admin"))
        existing_admin = result.scalar_one_or_none()

        if existing_admin:
            return None, None

        result = await session.execute(select(Role).where(Role.name == "Administrator"))
        admin_role = result.scalar_one_or_none()

        if not admin_role:
            admin_role = Role(
                name="Administrator",
                description="System Administrator role",
                is_system=True
            )
            session.add(admin_role)
            await session.flush()

        # Phase 311 (D-08): the admin password is REQUIRED in EVERY runtime
        # mode and comes from the environment — the deployment's .env for
        # Compose, or the indramesh-backend-auth Secret for Kubernetes. Compose
        # mode used to fall back to a generated password written to a
        # container-local artifact; that fallback is gone, so a missing or empty
        # value fails closed here, before any user row is created.
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            raise RuntimeError(
                "ADMIN_PASSWORD is required before initial-user creation "
                "(set it in the deployment's .env for Compose, or in the "
                "indramesh-backend-auth Secret for Kubernetes)"
            )

        admin_user = User(
            username="admin",
            email="admin@example.com",
            password_hash=hash_password(admin_password),
            is_active=True,
            is_superuser=True
        )
        session.add(admin_user)
        await session.flush()

        user_profile = UserProfile(
            user_id=admin_user.id,
            first_name="Admin",
            last_name="User"
        )
        session.add(user_profile)

        user_role = UserRole(
            user_id=admin_user.id,
            role_id=admin_role.id
        )
        session.add(user_role)

        await session.commit()

        return admin_password


async def run_init():
    async with async_session_maker() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

        result = await session.execute(select(Role))
        roles = result.scalars().all()

        if not users and not roles:
            logger.info("initializing default user and role")
            # Phase 311 (D-08): no plaintext password artifact is written in
            # ANY runtime mode. The operator supplies ADMIN_PASSWORD for the
            # first admin creation and changes it in the UI afterwards; the
            # former /tmp artifact and its delete-on-first-login path are gone.
            await init_default_user()
        else:
            logger.info("database already initialized with users and roles")


async def seed_starter_templates() -> None:
    from app.data import load_starter_templates

    starters = load_starter_templates()
    starter_names = {s["name"] for s in starters}

    async with async_session_maker() as session:
        result = await session.execute(
            select(SchemaTemplate.name).where(SchemaTemplate.name.in_(starter_names))
        )
        existing_names = set(result.scalars().all())

        to_seed = [s for s in starters if s["name"] not in existing_names]
        if to_seed:
            for s in to_seed:
                session.add(SchemaTemplate(**s))
            await session.commit()


# Seeded resource:action permissions (started at ROADMAP.md Phase 120, extended
# by every phase that added a resource — keep this list and the content/admin/
# runtime classification below in sync; the import-time asserts enforce it).
_PERMISSION_RESOURCES: list[dict[str, str | list[str]]] = [
    {"resource": "sources", "actions": ["create", "read", "update", "delete"]},
    {"resource": "schemas", "actions": ["create", "read", "update", "delete"]},
    {"resource": "object-types", "actions": ["create", "read", "update", "delete"]},
    {"resource": "objects", "actions": ["create", "read", "update", "delete"]},
    {"resource": "object-values", "actions": ["create", "read", "update", "delete"]},
    {"resource": "object-refs", "actions": ["create", "read", "update", "delete"]},
    {"resource": "attribute-types", "actions": ["create", "read", "update", "delete"]},
    {"resource": "object-type-attributes", "actions": ["create", "read", "update", "delete"]},
    {"resource": "reference-types", "actions": ["create", "read", "update", "delete"]},
    {"resource": "statuses", "actions": ["create", "read", "update", "delete"]},
    {"resource": "imports", "actions": ["create", "read", "update", "delete"]},
    {"resource": "exports", "actions": ["create", "read", "update", "delete"]},
    {"resource": "export-definitions", "actions": ["create", "read", "update", "delete"]},
    {"resource": "import-definitions", "actions": ["create", "read", "update", "delete"]},
    {"resource": "tasks", "actions": ["create", "read", "update", "delete"]},
    {"resource": "targets", "actions": ["create", "read", "update", "delete"]},
    {"resource": "service-accounts", "actions": ["create", "read", "update", "delete"]},
    {"resource": "api-clients", "actions": ["create", "read", "update", "revoke", "delete"]},
    {"resource": "icons", "actions": ["create", "read", "update", "delete"]},
    {"resource": "schema-templates", "actions": ["create", "read", "update", "delete"]},
    {"resource": "users", "actions": ["create", "read", "update", "delete", "manage"]},
    {"resource": "roles", "actions": ["create", "read", "update", "delete", "manage"]},
    {"resource": "sync", "actions": ["read", "trigger"]},
    {"resource": "config", "actions": ["read", "update"]},
    {"resource": "saved-queries", "actions": ["create", "read", "update", "delete", "execute"]},
    {"resource": "schedules", "actions": ["create", "read", "update", "delete", "trigger"]},
    {"resource": "settings", "actions": ["read", "update"]},
    {"resource": "conflicts", "actions": ["read", "manage"]},
    {"resource": "audit-log", "actions": ["read"]},
    {"resource": "audit-log-config", "actions": ["read", "update"]},
    # Phase 305 (LOGX-05/Carry-02): `system-logs:read` is no longer a Core-seeded
    # permission — the log-explorer plugin owns the name (manifest declaration +
    # the 305 ownership-transfer migration). Seeding it here would collide with
    # the plugin's declaration and refuse the whole plugin (SEAM-01/D-04).
    {"resource": "search", "actions": ["read", "trigger", "manage"]},
    {"resource": "languages", "actions": ["create", "read", "update", "delete"]},
    {"resource": "translations", "actions": ["create", "read", "update", "delete"]},
    # 246 D-16: groups:manage gates both membership directions
    # (POST/DELETE /groups/{id}/members/{uid} and /users/{id}/groups/{gid}).
    # Group->role assignment stays gated by roles:manage (Rollen-Bearbeitung).
    {"resource": "groups", "actions": ["create", "read", "update", "delete", "manage"]},
    # Phase 282 (PLUGCORE-09/10): plugin management. `read` covers
    # list/detail/health, `manage` covers the config API and (Plan 282-08)
    # the kill-switch.
    {"resource": "plugins", "actions": ["read", "manage"]},
    # Phase 289 (CORESEAM-09/D-05): using a plugin's runtime surfaces (mounted
    # routes and served bundles) is its own NON-admin capability. `use` is the
    # only action — there is deliberately no `read`/`manage` variant, and the
    # runtime gate must never be satisfied by the admin `plugins:*` grants.
    {"resource": "plugin-runtime", "actions": ["use"]},
]


async def seed_default_permissions() -> None:
    """Seed default resource:action permissions if they don't exist.

    Idempotent: skips permissions whose name already exists in the DB.
    Safe to call on every startup — only inserts missing permissions.
    """
    async with async_session_maker() as session:
        # Fetch existing permission names in one query
        result = await session.execute(select(Permission.name))
        existing_names: set[str] = set(result.scalars().all())

        to_seed: list[Permission] = []
        for entry in _PERMISSION_RESOURCES:
            resource = entry["resource"]
            for action in entry["actions"]:
                perm_name = f"{resource}:{action}"
                if perm_name not in existing_names:
                    to_seed.append(Permission(
                        name=perm_name,
                        resource=resource,
                        action=action,
                        description=f"Allows {action} on {resource}"
                    ))

        if to_seed:
            session.add_all(to_seed)
            await session.commit()


# Content-entity resources — full CRUD for Editor role (D-01, D-03)
_CONTENT_RESOURCES: set[str] = {
    "schemas", "object-types", "objects", "object-values", "object-refs",
    "attribute-types", "object-type-attributes", "reference-types", "statuses", "imports", "exports",
    "export-definitions", "import-definitions",
    "tasks", "icons", "saved-queries", "schema-templates", "groups",
}

# Admin-only resources — Editor gets no permissions on these (D-02)
_ADMIN_RESOURCES: set[str] = {
    "sources", "targets", "service-accounts", "api-clients", "sync", "config",
    "schedules", "users", "roles", "conflicts", "settings",
    "audit-log", "audit-log-config", "search", "languages",
    "translations",
    # Phase 282: plugin management is admin-only — the seeded Editor role must
    # NOT gain it (Plan 282-07's RBAC tests rely on Editor getting 403).
    "plugins",
}

# Runtime-use resources — Plan 289-08 (CORESEAM-09/D-05): granted to EVERY
# default role because using a plugin is a normal product capability, not an
# administrative one. Kept as its own class (not folded into _CONTENT_RESOURCES)
# so the exhaustive classification below keeps proving that every seeded
# resource is deliberately placed, and so `plugins` never becomes a content
# resource by accident.
_RUNTIME_RESOURCES: set[str] = {
    "plugin-runtime",
}

# Admin resources the Viewer still reads: the UI dictionary
# (`/ui-translations/dictionary`) and the language list are gated by these
# and carry no secrets. Every other admin resource stays unreadable (TRI317-51).
_VIEWER_REFERENCE_RESOURCES: set[str] = {"languages", "translations"}
assert _VIEWER_REFERENCE_RESOURCES <= _ADMIN_RESOURCES, \
    "_VIEWER_REFERENCE_RESOURCES must name admin resources"

# 304 (GQL-03/D-07): `saved-queries:execute` is a Core permission that is
# Administrator-only by default even though `saved-queries` is a content
# resource. `saved-queries` stays in `_CONTENT_RESOURCES` so Editor keeps
# create/read/update/delete; this set subtracts the execute action from the
# Editor grant loop below (and is never emitted for Viewer, which only gets
# `:read`). Other roles can still be granted it administratively.
#
# 261022 (user decision): starting, changing or deleting an import writes
# objects WITHOUT the per-object `objects:create/update` checks and the
# record/attribute write DENYs of the acting user, so those three actions are
# Administrator-only by default too. `imports` stays a content resource
# (Editor keeps `imports:read`, Viewer keeps its generic `:read`) — only the
# write actions are subtracted. Definitions stay content: defining a mapping
# writes no object; running one needs `imports:create` on every path.
_ADMIN_ONLY_ACTIONS: set[str] = {
    "saved-queries:execute",
    "imports:create",
    "imports:update",
    "imports:delete",
    # TRI317-50: a group carries roles, so whoever manages groups and their
    # members hands out roles. Role and permission changes are
    # Administrator-only; Editor keeps groups:read.
    "groups:create",
    "groups:update",
    "groups:delete",
    "groups:manage",
}

# The exclusion set must name actions that a content resource actually
# declares, otherwise the subtraction would be dead code / typo'd.
_ALL_ACTION_NAMES: set[str] = {
    f"{entry['resource']}:{action}"
    for entry in _PERMISSION_RESOURCES
    for action in entry["actions"]
}
assert _ADMIN_ONLY_ACTIONS <= _ALL_ACTION_NAMES, \
    "_ADMIN_ONLY_ACTIONS must name actions declared in _PERMISSION_RESOURCES"

# Safety net: fail loudly at import time if _PERMISSION_RESOURCES gains/loses
# a resource without the content/admin/runtime sets being updated to match —
# otherwise a new resource would silently get zero Editor/Viewer coverage.
_ALL_RESOURCE_NAMES: set[str] = {entry["resource"] for entry in _PERMISSION_RESOURCES}
assert not (_CONTENT_RESOURCES & _ADMIN_RESOURCES), \
    "_CONTENT_RESOURCES and _ADMIN_RESOURCES must not overlap"
assert not (_CONTENT_RESOURCES & _RUNTIME_RESOURCES), \
    "_CONTENT_RESOURCES and _RUNTIME_RESOURCES must not overlap"
assert not (_ADMIN_RESOURCES & _RUNTIME_RESOURCES), \
    "_ADMIN_RESOURCES and _RUNTIME_RESOURCES must not overlap"
assert _CONTENT_RESOURCES | _ADMIN_RESOURCES | _RUNTIME_RESOURCES == _ALL_RESOURCE_NAMES, \
    "_CONTENT_RESOURCES + _ADMIN_RESOURCES + _RUNTIME_RESOURCES must cover every " \
    "resource in _PERMISSION_RESOURCES exactly once"


def core_permission_names() -> frozenset[str]:
    """Every seeded Core ``<resource>:<action>`` permission name.

    The public single source of truth for the static Core permission catalogue
    (review correction R01-4): the plugin permission registrar and the
    task-type/per-route validation consume this helper instead of importing
    ``_PERMISSION_RESOURCES`` directly. A plugin declaring one of these names is
    refused as a Core ownership collision (D-04).
    """
    return frozenset(
        f"{entry['resource']}:{action}"
        for entry in _PERMISSION_RESOURCES
        for action in entry["actions"]
    )


async def seed_default_roles() -> None:
    """Seed three default roles — Administrator, Editor, Viewer — with
    RolePermission grants.

    Idempotent: skips roles and RolePermission assignments that already
    exist. Safe to call on every startup. Must be called AFTER
    seed_default_permissions() so the permissions table is populated.
    """
    async with async_session_maker() as session:
        # ── Step 1: Ensure all three roles exist ──
        result = await session.execute(select(Role.name))
        existing_role_names: set[str] = set(result.scalars().all())

        role_defs = [
            Role(
                name="Administrator",
                description="System administrator with all permissions",
                is_system=True,
            ),
            Role(
                name="Editor",
                description="Can create, read, update, and delete content entities",
                is_system=True,
            ),
            Role(
                name="Viewer",
                description="Read-only access to all entities",
                is_system=True,
            ),
        ]

        roles_to_create = [r for r in role_defs if r.name not in existing_role_names]
        if roles_to_create:
            session.add_all(roles_to_create)
            await session.flush()

        # Re-fetch all three roles (needed for their IDs)
        result = await session.execute(select(Role).where(Role.name.in_(
            {"Administrator", "Editor", "Viewer"}
        )))
        roles: dict[str, Role] = {r.name: r for r in result.scalars().all()}

        # ── Step 2: Fetch all permission names → Permission ID map ──
        result = await session.execute(select(Permission))
        all_permissions = result.scalars().all()
        perm_by_name: dict[str, Permission] = {p.name: p for p in all_permissions}

        # ── Step 3: Fetch existing RolePermission rows (idempotency) ──
        role_ids = [r.id for r in roles.values()]
        result = await session.execute(
            select(RolePermission.role_id, RolePermission.permission_id)
            .where(RolePermission.role_id.in_(role_ids))
        )
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set(result.all())

        # ── Step 4: Determine permission sets per role ──
        def _perm_names_for_resource(resource_cfg: dict) -> list[str]:
            return [f"{resource_cfg['resource']}:{a}" for a in resource_cfg["actions"]]

        all_perm_names: list[str] = []
        for entry in _PERMISSION_RESOURCES:
            all_perm_names.extend(_perm_names_for_resource(entry))

        # Runtime-use resources go to EVERY default role (CORESEAM-09/D-05):
        # Administrator already covers them through all_perm_names; Editor and
        # Viewer receive them explicitly just below.
        runtime_perm_names: list[str] = []
        for entry in _PERMISSION_RESOURCES:
            if entry["resource"] in _RUNTIME_RESOURCES:
                runtime_perm_names.extend(_perm_names_for_resource(entry))

        # Editor: CRUD on content resources only, plus plugin runtime use.
        # 304 (D-07) / 261022: admin-only actions are subtracted so the Editor
        # role never gains `saved-queries:execute` or the import write actions
        # through the content-resource rule.
        editor_perm_names: list[str] = []
        for entry in _PERMISSION_RESOURCES:
            if entry["resource"] in _CONTENT_RESOURCES:
                editor_perm_names.extend(_perm_names_for_resource(entry))
        editor_perm_names = [
            name for name in editor_perm_names if name not in _ADMIN_ONLY_ACTIONS
        ]
        editor_perm_names.extend(runtime_perm_names)

        # Viewer: read on the content resources plus the UI-language reference
        # data, plus plugin runtime use. TRI317-51: never read on the other
        # admin resources (audit-log, users, roles, api-clients, sync, ...) —
        # the Viewer must not read more than the Editor.
        viewer_perm_names: list[str] = [
            f"{entry['resource']}:read"
            for entry in _PERMISSION_RESOURCES
            if entry["resource"] in _CONTENT_RESOURCES | _VIEWER_REFERENCE_RESOURCES
            and "read" in entry["actions"]
        ]
        viewer_perm_names.extend(runtime_perm_names)

        role_permission_map: dict[str, list[str]] = {
            "Administrator": all_perm_names,
            "Editor": editor_perm_names,
            "Viewer": viewer_perm_names,
        }

        # ── Step 5: Build RolePermission inserts ──
        to_insert: list[RolePermission] = []
        for role_name, perm_names in role_permission_map.items():
            role = roles.get(role_name)
            if not role:
                logger.warning("role %r not found, skipping permissions", role_name)
                continue
            for perm_name in perm_names:
                perm = perm_by_name.get(perm_name)
                if not perm:
                    # Permission may not exist yet if seed_default_permissions
                    # hasn't run — but we're called after it, so this shouldn't happen
                    logger.warning("permission %r not found, skipping", perm_name)
                    continue
                # Phase 303 (SEAM-01/D-01, review R01-5): plugin-owned rows are
                # never reseeded. A restart must not restore a plugin grant an
                # administrator removed, and a plugin's manifest defaults are
                # applied only at its first insert.
                if perm.owner_plugin is not None:
                    continue
                if (role.id, perm.id) not in existing:
                    to_insert.append(RolePermission(
                        role_id=role.id,
                        permission_id=perm.id,
                        is_deny=False,
                    ))

        if to_insert:
            session.add_all(to_insert)
            await session.commit()


async def seed_languages() -> None:
    """Seed the six bundled interface languages (de/en/es/fr/it/pt).

    Codes and English display names come from the single bundled-language map in
    ``app.data``, so the seed list cannot drift from the list the rest of the
    application treats as bundled. Display names are English because they appear
    identically in every language picker (D-06). The API sorts its response by
    ``code`` (de, en, es, fr, it, pt), so insertion order carries no meaning.

    Idempotent: skips codes that already exist. Safe to call on every startup.
    Per research: seeded at startup, not via Alembic data insert.
    """
    from app.data import BUNDLED_LANGUAGE_CODES, BUNDLED_LANGUAGE_NAMES

    async with async_session_maker() as session:
        result = await session.execute(select(Language.code))
        existing_codes: set[str] = set(result.scalars().all())

        base_languages = [
            {"code": code, "name": BUNDLED_LANGUAGE_NAMES[code]}
            for code in BUNDLED_LANGUAGE_CODES
        ]
        to_seed = [Language(**lang) for lang in base_languages if lang["code"] not in existing_codes]
        if to_seed:
            session.add_all(to_seed)
            await session.commit()


async def seed_translation_types() -> None:
    """Seed the eight translation types (D-14).

    Idempotent: skips codes that already exist. Safe to call on every startup.
    Seeded at startup, not via Alembic data insert (mirrors seed_languages).
    All eight types are seeded now; the /translations endpoint only accepts
    object_type / object_type_attribute until Phases 222-224.
    """
    async with async_session_maker() as session:
        result = await session.execute(select(TranslationType.code))
        existing_codes: set[str] = set(result.scalars().all())

        base_types = [
            {"code": "object_type", "label": "Object Type"},
            {"code": "object_type_attribute", "label": "Object Type Attribute"},
            {"code": "status", "label": "Status"},
            {"code": "reference_type", "label": "Reference Type"},
            {"code": "select_option", "label": "Select Option"},
            {"code": "object_value", "label": "Object Value"},
            {"code": "systemui", "label": "UI String"},
            {"code": "classification_label", "label": "Classification Label"},
        ]
        to_seed = [TranslationType(**t) for t in base_types if t["code"] not in existing_codes]
        if to_seed:
            session.add_all(to_seed)
            await session.commit()


# The three system families and their default labels (D-02, D-04, D-05, D-12,
# D-13). ``position`` doubles as rank inside a ranked family (higher = stricter,
# D-10). ``de`` is seeded as the ``name`` translation of the label; the base
# ``name`` stays English. Labels default to ``is_system=False`` so an operator
# may delete them (D-14) — only the personal-data label is fixed.
_CLASSIFICATION_CATALOG: tuple[dict, ...] = (
    {
        "key": "confidentiality",
        "name": "Confidentiality",
        "is_multi_valued": False,
        "is_ranked": True,
        "allows_operator_labels": True,
        "sort": 0,
        "labels": (
            {"key": "public", "name": "Public", "de": "Öffentlich", "position": 0, "is_system": False},
            {"key": "internal", "name": "Internal", "de": "Intern", "position": 1, "is_system": False},
            {"key": "confidential", "name": "Confidential", "de": "Vertraulich", "position": 2, "is_system": False},
            {"key": "strictly_confidential", "name": "Strictly confidential", "de": "Streng vertraulich", "position": 3, "is_system": False},
        ),
    },
    {
        "key": "personal_data",
        "name": "Personal data",
        "is_multi_valued": False,
        "is_ranked": False,
        "allows_operator_labels": False,
        "sort": 1,
        "labels": (
            {"key": "personal_data", "name": "personal data", "de": "personenbezogen", "position": 0, "is_system": True},
        ),
    },
    {
        "key": "export_control",
        "name": "Export control",
        "is_multi_valued": True,
        "is_ranked": False,
        "allows_operator_labels": True,
        "sort": 2,
        "labels": (
            {"key": "dual_use_eu", "name": "Dual-Use (EU)", "de": "Dual-Use (EU)", "position": 0, "is_system": False},
            {"key": "national_list_al", "name": "National list (AL)", "de": "Nationale Liste (AL)", "position": 1, "is_system": False},
            {"key": "ear", "name": "EAR", "de": "EAR", "position": 2, "is_system": False},
            {"key": "itar", "name": "ITAR", "de": "ITAR", "position": 3, "is_system": False},
        ),
    },
)


async def ensure_classification_catalog(session) -> dict[str, int]:
    """Insert the missing system families, system labels and German label names.

    Flush-only; the caller commits. Default labels are inserted only together
    with a new family — a default an operator deleted is never re-created, and
    an existing family only receives its missing ``is_system`` labels. German
    ``name`` translations are insert-missing and never overwrite an existing row.
    """
    counts = {"families": 0, "labels": 0, "translations": 0}

    existing_families = {
        family.key: family
        for family in (await session.execute(select(ClassificationFamily))).scalars().all()
    }

    for family_spec in _CLASSIFICATION_CATALOG:
        family = existing_families.get(family_spec["key"])
        if family is None:
            family = ClassificationFamily(
                key=family_spec["key"],
                name=family_spec["name"],
                is_multi_valued=family_spec["is_multi_valued"],
                is_ranked=family_spec["is_ranked"],
                is_system=True,
                allows_operator_labels=family_spec["allows_operator_labels"],
                sort=family_spec["sort"],
            )
            session.add(family)
            await session.flush()
            counts["families"] += 1
            labels_to_create = list(family_spec["labels"])
        else:
            labels_to_create = [label for label in family_spec["labels"] if label["is_system"]]
            if labels_to_create:
                existing_keys = set(
                    (
                        await session.execute(
                            select(ClassificationLabel.key).where(
                                ClassificationLabel.family_id == family.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                labels_to_create = [
                    label for label in labels_to_create if label["key"] not in existing_keys
                ]

        for label_spec in labels_to_create:
            session.add(
                ClassificationLabel(
                    family_id=family.id,
                    family_multi_valued=family.is_multi_valued,
                    key=label_spec["key"],
                    name=label_spec["name"],
                    position=label_spec["position"],
                    is_system=label_spec["is_system"],
                )
            )
            counts["labels"] += 1
        if labels_to_create:
            await session.flush()

    family_key_by_id = {
        family.id: family.key
        for family in (await session.execute(select(ClassificationFamily))).scalars().all()
    }
    label_by_spec = {
        (family_key_by_id[label.family_id], label.key): label
        for label in (await session.execute(select(ClassificationLabel))).scalars().all()
        if label.family_id in family_key_by_id
    }

    translation_type = (
        await session.execute(
            select(TranslationType).where(TranslationType.code == "classification_label")
        )
    ).scalar_one_or_none()
    german = (
        await session.execute(select(Language).where(Language.code == "de"))
    ).scalar_one_or_none()

    if translation_type is not None and german is not None and label_by_spec:
        existing_entities = set(
            (
                await session.execute(
                    select(Translation.entity_id).where(
                        Translation.translation_type_id == translation_type.id,
                        Translation.entity_id.in_(
                            [label.id for label in label_by_spec.values()]
                        ),
                        Translation.field == "name",
                        Translation.language_id == german.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for family_spec in _CLASSIFICATION_CATALOG:
            for label_spec in family_spec["labels"]:
                label = label_by_spec.get((family_spec["key"], label_spec["key"]))
                if label is None or label.id in existing_entities:
                    continue
                session.add(
                    Translation(
                        translation_type_id=translation_type.id,
                        entity_id=label.id,
                        field="name",
                        language_id=german.id,
                        value=label_spec["de"],
                    )
                )
                counts["translations"] += 1
        if counts["translations"]:
            await session.flush()

    return counts


async def seed_classification_catalog() -> None:
    """Own-session startup wrapper around ``ensure_classification_catalog``.

    Commits what it created and logs the counts; a concurrent starter that wins
    the ``UNIQUE`` race only rolls back and continues (RESEARCH Pattern 3.5).
    """
    async with async_session_maker() as session:
        try:
            counts = await ensure_classification_catalog(session)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            logger.warning(
                "Classification catalogue seed raced another startup; existing rows kept"
            )
            return
    if any(counts.values()):
        logger.info("Seeded classification catalogue: %s", counts)


async def seed_canonical_attribute_types() -> None:
    """Seed the 16 canonical attribute_types rows (D-11/D-14), idempotent.

    Mirrors seed_languages/seed_translation_types (diff-by-key, insert only
    what's missing). The 245-01 migration already normalized the live
    catalog to one immutable row per data_type; this covers the ongoing
    half of the same invariant — a fresh/empty database (or one where a
    canonical row was somehow lost) gets the full 16-row catalog back on
    the next startup, using the same name map the migration and
    attribute_type_service.get_or_create_canonical_by_data_type both use.
    """
    from app.services.attribute_type_service import CANONICAL_ATTRIBUTE_TYPE_NAMES

    async with async_session_maker() as session:
        result = await session.execute(select(AttributeType.data_type))
        existing_data_types: set[str] = set(result.scalars().all())

        to_seed = [
            AttributeType(name=name, data_type=data_type)
            for data_type, name in CANONICAL_ATTRIBUTE_TYPE_NAMES.items()
            if data_type not in existing_data_types
        ]
        if to_seed:
            session.add_all(to_seed)
            await session.commit()


# D-12 (Phase 323.1): the startup prune only runs when the *loaded* seed
# catalog carries at least this many keys. A shorter list is treated as
# truncated/vacuous — the same 1000-key threshold the lean-core locale gate
# uses (backend/tests/test_core_lean_gate.py: MIN_LOCALE_CATALOG_KEYS), where a
# catalog below it is a defect rather than a clean pass. Never compare against
# a constant seed inventory: the point is to catch a damaged load.
PRUNE_MIN_KEYS = 1000


async def seed_ui_strings() -> bool:
    """Idempotent startup seed for the ui_strings key registry.

    Skips keys that already exist (diff-by-key, mirrors seed_languages /
    seed_translation_types). Repairs default_text on existing keys whose
    code-side default changed. ``seed_ui_catalog()`` invalidates the
    ui_translations cache tag once when this registry or its English systemui
    rows change; a no-op re-seed leaves the cache intact.

    Since Phase 323.1 (I18N-03 / D-11..D-13) the registry is also pruned: every
    ui_strings row whose key is no longer in the committed seed list is deleted,
    together with its ``systemui`` translations — including orphaned systemui
    rows from earlier deletions, so ``coverage.translated`` and the denominator
    of the coverage can no longer disagree. The prune is one-way (no soft
    delete, no retention window) and guarded by ``PRUNE_MIN_KEYS``: a seed list
    shorter than that means the catalog failed to load completely, and then
    nothing is deleted. Keys that stay, and their admin-edited translations,
    are never touched.

    Per D-01/D-02 (Phase 230 CONTEXT.md), the seed list in
    backend/app/data/ui_strings.json is deliberately near-empty in this
    phase — the real key inventory is produced by Phase 231's i18next-cli
    extraction pass, not hand-written here. The repair step must never
    touch the translations table (T-230-01) — it only updates
    ui_strings.default_text.
    """
    from app.data import load_ui_strings
    seed_items = load_ui_strings()
    registry_changed = False

    async with async_session_maker() as session:
        result = await session.execute(select(UiString.key, UiString.default_text))
        existing_rows: dict[str, str] = {key: default_text for key, default_text in result.all()}

        to_seed = [UiString(**item) for item in seed_items if item["key"] not in existing_rows]
        if to_seed:
            session.add_all(to_seed)
            await session.commit()
            registry_changed = True

        # default_text repair for existing keys whose seed default changed —
        # scoped strictly to ui_strings, never touches translations rows.
        changed = False
        for item in seed_items:
            key = item["key"]
            if key in existing_rows and existing_rows[key] != item["default_text"]:
                await session.execute(
                    update(UiString)
                    .where(UiString.key == key)
                    .values(default_text=item["default_text"])
                )
                changed = True
        if changed:
            await session.commit()
            registry_changed = True

        # --- Prune (I18N-03 / D-11..D-13) -------------------------------------
        # A key that left the code must leave the registry. Candidates are
        # derived in Python from the rows already loaded above — sending a
        # NOT IN list with >1700 bound parameters would be pointless traffic.
        # Guard BEFORE any delete: a truncated catalog deletes nothing.
        seed_keys = {item["key"] for item in seed_items}
        if len(seed_keys) >= PRUNE_MIN_KEYS:
            orphan_keys = [key for key in existing_rows if key not in seed_keys]
            deleted_keys = 0
            if orphan_keys:
                delete_result = await session.execute(
                    delete(UiString).where(UiString.key.in_(orphan_keys))
                )
                deleted_keys = delete_result.rowcount or 0

            # Orphaned systemui rows (no ui_strings row) count towards
            # coverage.translated but render nowhere. The subquery sees the
            # deletion above — same transaction — so the keys removed just now
            # and older orphans are cleaned in this single statement. Scoped
            # strictly to the systemui type; other translation families that
            # share an entity_id are not ours to touch.
            deleted_translations = 0
            type_result = await session.execute(
                select(TranslationType).where(TranslationType.code == "systemui")
            )
            systemui_type = type_result.scalar_one_or_none()
            if systemui_type is not None:
                delete_result = await session.execute(
                    delete(Translation).where(
                        Translation.translation_type_id == systemui_type.id,
                        Translation.entity_id.not_in(select(UiString.id)),
                    )
                )
                deleted_translations = delete_result.rowcount or 0

            await session.commit()
            if deleted_keys or deleted_translations:
                registry_changed = True
            # Exactly one prune line per start on this path — including the
            # no-op case — so a log grep for the prune prefix is deterministic.
            # Counters only: never a key or a translation value.
            logger.info(
                "ui_strings prune: deleted %d keys, %d translations",
                deleted_keys,
                deleted_translations,
            )
        else:
            # Nothing is deleted; the single guard line is the whole log output.
            logger.warning(
                "ui_strings prune: skipped — seed list %d keys, guard %d",
                len(seed_keys),
                PRUNE_MIN_KEYS,
            )

    return registry_changed


async def seed_ui_translations() -> bool:
    """Seed missing English ``systemui`` translations from the UI registry.

    The translations table is polymorphic, so the presence of rows for a data
    translation type must not satisfy the UI seed check.  Existing systemui
    rows are deliberately left untouched because they may contain an admin's
    custom value.
    """
    from app.services.cache_service import invalidate_entity_cache

    async with async_session_maker() as session:
        type_result = await session.execute(
            select(TranslationType).where(TranslationType.code == "systemui")
        )
        systemui_type = type_result.scalar_one_or_none()
        if systemui_type is None:
            return False

        language_result = await session.execute(
            select(Language).where(Language.code == "en")
        )
        english = language_result.scalar_one_or_none()
        if english is None:
            return False

        ui_strings_result = await session.execute(select(UiString))
        ui_strings = ui_strings_result.scalars().all()
        if not ui_strings:
            return False

        ui_string_ids = [ui_string.id for ui_string in ui_strings]
        existing_result = await session.execute(
            select(Translation.entity_id).where(
                Translation.translation_type_id == systemui_type.id,
                Translation.entity_id.in_(ui_string_ids),
                Translation.field == "value",
                Translation.language_id == english.id,
            )
        )
        existing_entity_ids = set(existing_result.scalars().all())

        missing = [
            Translation(
                translation_type_id=systemui_type.id,
                entity_id=ui_string.id,
                field="value",
                language_id=english.id,
                value=ui_string.default_text,
            )
            for ui_string in ui_strings
            if ui_string.id not in existing_entity_ids
        ]
        if missing:
            session.add_all(missing)
            await session.commit()
            return True

    return False


# G-279-2 gap closure: locales whose bundled backend/app/data/ui_translations_{code}.json
# catalog is seeded into the systemui Translation table at startup, mirroring
# seed_ui_translations()'s English-row seed for each code. Purely additive —
# extend this tuple (and commit the matching JSON file) to onboard a future
# locale with zero further init_service.py changes.
#
# Each bundled catalog is the complete mirror of frontend/locales/{code}/systemui.json
# (every UI key), so a fresh database renders the whole interface in that language.
# A partial catalog silently renders the unseeded remainder in English while the
# locale file still looks fully translated — backend/tests/test_ui_inventory_parity.py
# pins the two together.
_UI_TRANSLATION_LOCALE_CODES = ("de", "es", "fr", "it", "pt")


async def seed_ui_translations_locales() -> bool:
    """Seed missing non-English ``systemui`` translations from the bundled
    per-locale catalogs in ``backend/app/data/ui_translations_{code}.json``.

    Generic over ``_UI_TRANSLATION_LOCALE_CODES`` — no per-language branching.
    Mirrors seed_ui_translations()'s non-destructive contract: an existing
    systemui row for a given key/language (possibly an admin edit made via
    the Translations UI) is never touched, only missing rows are inserted.
    A locale with no bundled catalog file, an empty catalog, or no matching
    Language row is simply skipped — never an error.
    """
    from app.data import load_ui_translations_locale

    async with async_session_maker() as session:
        type_result = await session.execute(
            select(TranslationType).where(TranslationType.code == "systemui")
        )
        systemui_type = type_result.scalar_one_or_none()
        if systemui_type is None:
            return False

        ui_strings_result = await session.execute(select(UiString))
        ui_strings = ui_strings_result.scalars().all()
        if not ui_strings:
            return False
        ui_string_by_key = {ui_string.key: ui_string for ui_string in ui_strings}

        any_changed = False
        for code in _UI_TRANSLATION_LOCALE_CODES:
            locale_catalog = load_ui_translations_locale(code)
            if not locale_catalog:
                continue

            language_result = await session.execute(
                select(Language).where(Language.code == code)
            )
            language = language_result.scalar_one_or_none()
            if language is None:
                continue

            locale_ui_string_ids = [
                ui_string_by_key[key].id
                for key in locale_catalog
                if key in ui_string_by_key
            ]
            if not locale_ui_string_ids:
                continue

            existing_result = await session.execute(
                select(Translation.entity_id).where(
                    Translation.translation_type_id == systemui_type.id,
                    Translation.entity_id.in_(locale_ui_string_ids),
                    Translation.field == "value",
                    Translation.language_id == language.id,
                )
            )
            existing_entity_ids = set(existing_result.scalars().all())

            missing = [
                Translation(
                    translation_type_id=systemui_type.id,
                    entity_id=ui_string_by_key[key].id,
                    field="value",
                    language_id=language.id,
                    value=value,
                )
                for key, value in locale_catalog.items()
                if key in ui_string_by_key and ui_string_by_key[key].id not in existing_entity_ids
            ]
            if missing:
                session.add_all(missing)
                await session.commit()
                any_changed = True

        return any_changed


async def seed_ui_catalog() -> None:
    registry_changed = await seed_ui_strings()
    translations_changed = await seed_ui_translations()
    locales_changed = await seed_ui_translations_locales()
    if registry_changed or translations_changed or locales_changed:
        from app.services.cache_service import invalidate_entity_cache
        await invalidate_entity_cache("ui_translations")


async def seed_audit_log_config() -> None:
    """Seed all audit entity type config rows.
    Disabled by default, except ALWAYS_ON_ENTITY_TYPES (audit_log_config,
    settings, permissions, users, roles, api_clients) which seed enabled and
    can't be switched off via the API.
    Idempotent: skips rows whose entity_type already exists, then repairs any
    pre-existing ALWAYS_ON row that was seeded disabled in an earlier phase
    (WR-03 drift repair — the api_clients row seeded before Plan 200-05 must
    flip to enabled without a migration).
    Per D-02: called at every startup — seeded not lazy-created.
    """
    from app.models.audit_log_config import AuditLogConfig
    from app.services.audit_service import AuditEntityType, ALWAYS_ON_ENTITY_TYPES

    async with async_session_maker() as session:
        result = await session.execute(
            select(AuditLogConfig.entity_type)
        )
        existing = set(result.scalars().all())

        # WR-03 drift repair — seed_audit_log_config used to skip existing
        # rows, so a live DB seeded before api_clients joined
        # ALWAYS_ON_ENTITY_TYPES still has audit_log_enabled=False. Flip every
        # existing ALWAYS_ON row that is currently disabled (idempotent).
        if existing:
            await session.execute(
                update(AuditLogConfig)
                .where(
                    AuditLogConfig.entity_type.in_(sorted(ALWAYS_ON_ENTITY_TYPES)),
                    AuditLogConfig.audit_log_enabled == False,
                )
                .values(audit_log_enabled=True)
            )
            await session.commit()

        to_seed = [
            AuditLogConfig(
                entity_type=et.value,
                audit_log_enabled=et.value in ALWAYS_ON_ENTITY_TYPES,
            )
            for et in AuditEntityType
            if et.value not in existing
        ]
        if to_seed:
            session.add_all(to_seed)
            await session.commit()


async def init_db_on_startup():
    await run_init()
    await seed_starter_templates()
    await seed_languages()
    await seed_translation_types()
    await seed_classification_catalog()  # 317.1 — seed the classification label catalogue
    await seed_canonical_attribute_types()  # Phase 245 — seed canonical attribute_types catalog (D-11/D-14)
    await seed_ui_catalog()  # Phase 230/231 — seed UI registry and English systemui rows
    await seed_default_permissions()  # Phase 120 — seed permissions
    await seed_default_roles()        # Phase 126 — seed default roles and their RolePermission grants
    await seed_audit_log_config()     # Phase 166 — seed audit log config rows
