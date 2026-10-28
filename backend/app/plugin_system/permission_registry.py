"""Discovery-time registration of plugin-declared permissions (SEAM-01).

The manifest is DATA, but a declared ``[[permissions]]`` entry becomes a
persisted Core ``permissions`` row owned by its declaring plugin
(``owner_plugin``). Two invariants make this safe across restarts, plugin
updates and reactivation:

* **Insert-only defaults.** Default-role grants are created only for the rows
  the ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` actually created. A
  later discovery therefore never restores an administratively removed grant
  (D-01).
* **Whole-plugin refusal on a collision.** A name already owned by Core or by
  another plugin refuses the entire declaring plugin (D-04); existing rows and
  grants are never relabelled, renamed or deleted.

Only the backend process performs the write (review correction R01-1): the
worker runs the same validation/collision check read-only via ``write=False``.
"""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import Permission, RolePermission
from app.models.role import Role
from app.plugin_system.manifest import CORE_ROLE_NAMES, PluginPermissionDeclaration

logger = logging.getLogger(__name__)


class PermissionCollisionError(ValueError):
    """A declared permission name is already owned by Core or another plugin."""

    def __init__(self, name: str, owner: str) -> None:
        self.name = name
        self.owner = owner
        super().__init__(f"permission {name!r} is already owned by {owner}")


def owner_display(owner_plugin: str | None) -> str:
    """Human-readable owner label used in collision diagnostics."""
    return "Core" if owner_plugin is None else f"plugin {owner_plugin!r}"


def known_permission_names(manifest) -> frozenset[str]:
    """The validated catalogue for one declaring owner (review R01-4/R02-4).

    Core's static catalogue PLUS only this manifest's own declared permissions —
    never another plugin's. Consumed by the route gate and the task-type
    ``required_permission`` validation, so both accept a plugin's own declared
    permission and reject a foreign/unknown one at the same boundary.
    """
    from app.services.init_service import core_permission_names

    names = set(core_permission_names())
    if manifest is not None:
        names.update(
            declaration.name for declaration in (manifest.permissions or [])
        )
    return frozenset(names)


async def register_declared_permissions(
    session: AsyncSession,
    plugin_name: str,
    declarations: list[PluginPermissionDeclaration],
    *,
    write: bool,
) -> list[str]:
    """Validate and (optionally) persist a plugin's declared permissions.

    ``write=False`` (the worker process) performs only the validation/collision
    read and inserts nothing. Returns the names of the permissions created by
    this call.

    Raises :class:`PermissionCollisionError` when a declared name is owned by
    Core (``owner_plugin IS NULL``) or by a different plugin — the caller must
    refuse the whole declaring plugin.
    """
    if not declarations:
        return []

    names = [declaration.name for declaration in declarations]

    # 0. Collision against the static Core catalogue even when the DB row is
    #    absent (D-04): a plugin may never claim a Core permission name, and a
    #    partially-seeded database must not open a hole. Imported lazily —
    #    init_service pulls the Core auth/model stack in.
    from app.services.init_service import core_permission_names

    core_names = core_permission_names()
    for name in names:
        if name in core_names:
            raise PermissionCollisionError(name, "Core")

    # 1. Collision against persisted ownership (including legacy NULL-owned
    #    Core rows). Existing rows/grants are never touched.
    existing_rows = (
        await session.execute(select(Permission).where(Permission.name.in_(names)))
    ).scalars().all()
    existing_by_name = {row.name: row for row in existing_rows}
    for name in names:
        row = existing_by_name.get(name)
        if row is not None and row.owner_plugin != plugin_name:
            raise PermissionCollisionError(name, owner_display(row.owner_plugin))

    if not write:
        return []

    # 2. Defence in depth (review correction R01-1b): never insert a permission
    #    whose declared default roles do not all exist yet. Otherwise a
    #    fresh-install race (worker ahead of Core role seeding) could consume
    #    the insert without its first-insert grants — a permission row without
    #    its defaults can never be re-granted. Deferred and retried next start.
    existing_roles = set(
        (
            await session.execute(
                select(Role.name).where(Role.name.in_(CORE_ROLE_NAMES))
            )
        ).scalars().all()
    )
    insertable: list[PluginPermissionDeclaration] = []
    for declaration in declarations:
        missing = [role for role in declaration.default_roles if role not in existing_roles]
        if missing:
            logger.warning(
                "Deferring permission %r for plugin %r: default role(s) %s do "
                "not exist yet — retrying on the next start",
                declaration.name,
                plugin_name,
                missing,
            )
            continue
        insertable.append(declaration)

    if not insertable:
        return []

    # 3. Create the still-missing rows; the RETURNING set identifies exactly the
    #    rows created in this call (never a pre-existing row).
    insert_stmt = (
        pg_insert(Permission)
        .values(
            [
                {
                    "name": declaration.name,
                    "resource": declaration.resource,
                    "action": declaration.action,
                    "description": declaration.description
                    or f"Declared by plugin {plugin_name}",
                    "owner_plugin": plugin_name,
                }
                for declaration in insertable
            ]
        )
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(Permission.id, Permission.name)
    )
    created = (await session.execute(insert_stmt)).all()
    if not created:
        return []
    created_by_name = {name: permission_id for permission_id, name in created}

    # 4. First-insert default grants: only for the created ids and only the
    #    roles explicitly named in each declaration (D-02 — no implicit class
    #    fallback, no over-permission).
    role_ids = {
        role_name: role_id
        for role_id, role_name in (
            await session.execute(
                select(Role.id, Role.name).where(Role.name.in_(existing_roles))
            )
        ).all()
    }
    grants: list[RolePermission] = []
    for declaration in insertable:
        permission_id = created_by_name.get(declaration.name)
        if permission_id is None:
            continue
        for role_name in declaration.default_roles:
            role_id = role_ids.get(role_name)
            if role_id is None:
                continue
            grants.append(
                RolePermission(
                    role_id=role_id,
                    permission_id=permission_id,
                    is_deny=False,
                )
            )
    if grants:
        session.add_all(grants)
    await session.flush()

    return list(created_by_name.keys())
