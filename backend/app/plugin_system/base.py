"""IndraMeshPlugin base class — capability registration (D-02) and hooks (D-04).

Capability routing truth is class inheritance: a capability interface declares
itself explicitly via a class keyword argument and is collected automatically,
so a new capability type needs one new class and zero registry/loader change
(PLUGCORE-02)::

    class SearchProviderPlugin(IndraMeshPlugin, capability="search_provider"):
        ...

Concrete plugin classes never pass the kwarg. ``_capability_name`` is inherited
through normal attribute lookup, so "is this class itself an interface" must be
tested as ``"_capability_name" in cls.__dict__`` — never via ``getattr``.
"""

from typing import Any

from sqlalchemy import MetaData

from app.plugin_system.manifest import PluginManifest


class IndraMeshPlugin:
    """Base class every IndraMesh plugin subclasses."""

    # capability name -> interface class, populated by __init_subclass__.
    _capability_interfaces: dict[str, type] = {}

    # Set by the loader after construction — manifest validation happens before
    # the plugin class is even resolved.
    manifest: PluginManifest | None = None

    # Phase 287 (PLUGDB-01, A-287-01): a database-owning plugin exposes its own
    # declarative ``MetaData`` here so the core runner can hand it to the shared
    # Alembic env as ``target_metadata``. Additive with a ``None`` default —
    # ``__init_subclass__`` below only reacts to the ``capability`` kwarg, so
    # this changes no existing plugin's behaviour.
    database_metadata: MetaData | None = None

    def __init_subclass__(cls, capability: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if capability is None:
            return

        existing = IndraMeshPlugin._capability_interfaces.get(capability)
        if existing is not None and not (
            existing.__module__ == cls.__module__
            and existing.__qualname__ == cls.__qualname__
        ):
            raise ValueError(
                f"Capability name {capability!r} is already claimed by "
                f"{existing.__module__}.{existing.__qualname__} — "
                f"{cls.__module__}.{cls.__qualname__} cannot re-register it"
            )

        cls._capability_name = capability
        IndraMeshPlugin._capability_interfaces[capability] = cls

    async def on_load(self, ctx: Any) -> None:
        return None

    async def on_unload(self, ctx: Any) -> None:
        return None

    async def health_check(self, ctx: Any) -> bool:
        return True

    async def on_object_created(self, ctx: Any, **kwargs: Any) -> None:
        """Observe an Object create inside the writer's transaction.

        Best-effort (D-05): the hook runs before the service's ``commit()``, so
        a later rollback cannot un-deliver an already-run hook. Payload (D-14):
        ``object_id``, ``schema_id``, ``object_type_id``, ``actor_id``,
        ``origin`` (``{kind, name, task_id}``), ``new_values`` (Object column
        snapshot) and the always-present ``changed_attribute_ids`` /
        ``attribute_changes`` (filled by an import row; empty on a plain
        REST/SDK create).
        """
        return None

    async def on_object_updated(self, ctx: Any, **kwargs: Any) -> None:
        """Observe an Object update (or an ObjectValue mutation) in-transaction.

        Best-effort (D-05): delivered before the first commit, so a later
        rollback does not retract it. Payload (D-14): ``object_id``,
        ``schema_id``, ``object_type_id``, ``actor_id``, ``origin``,
        ``old_values``/``new_values`` (Object column snapshots) and
        ``changed_attribute_ids`` / ``attribute_changes`` — the latter two carry
        ObjectValue snapshots (``{"value_simple": <label>}`` for a Name
        binding) and are empty for a plain Object-only update.
        """
        return None

    async def on_object_deleted(self, ctx: Any, **kwargs: Any) -> None:
        """Observe an Object delete before the row is removed (D-06).

        Fires before ``db.delete(obj)`` so an existing row is still readable via
        the borrowed session; it carries the old snapshot only (no
        attribute-change keys, which describe attribute mutation).
        """
        return None

    async def on_object_ref_created(self, ctx: Any, **kwargs: Any) -> None:
        """Observe an ObjectRef create in-transaction (D-01/D-05).

        Best-effort: delivered before the service's ``commit()``, so a later
        rollback does not retract it. Payload (D-14): ``object_id`` /
        ``schema_id`` / ``object_type_id`` of the primary source object,
        ``ref_id``, ``old_values`` (``None`` — no prior row) and ``new_values``
        (``model_to_dict`` snapshot of the created reference), plus ``actor_id``
        and ``origin``.
        """
        return None

    async def on_object_ref_updated(self, ctx: Any, **kwargs: Any) -> None:
        """Observe a single ObjectRef update in-transaction (D-01/D-02/D-03).

        A reference change is one update event, never a delete/create pair, and
        never a general ``on_object_updated``. Best-effort (D-05): delivered
        before ``commit()``. Payload (D-14): ``object_id`` /
        ``schema_id`` / ``object_type_id`` of the ORIGINAL source object (the
        pre-move endpoint), ``ref_id``, and full ``old_values``/``new_values``
        ``model_to_dict`` snapshots — both endpoint ids are present in each
        snapshot. A same-value PATCH is a no-op and fires no hook.
        """
        return None

    async def on_object_ref_deleted(self, ctx: Any, **kwargs: Any) -> None:
        """Observe an ObjectRef delete after audit and before ``db.delete`` (D-06).

        Best-effort (D-05): delivered before ``commit()``. Payload (D-14):
        ``object_id`` / ``schema_id`` / ``object_type_id`` of the primary source
        object, ``ref_id``, ``old_values`` (snapshot captured before the delete)
        and ``new_values`` (``None``).
        """
        return None

    async def on_language_deleted(self, ctx: Any, **kwargs: Any) -> None:
        """Observe the deletion of a Language AFTER the deleting transaction commits.

        Deliberately different from the object hooks: this one fires with
        ``active_session=None``, so ``ctx.db()`` hands the subscriber its OWN
        session and the subscriber commits itself. A plugin that cleans up its
        own rows must persist that work independently — sharing the caller's
        transaction would let a plugin-side DB failure abort the language delete
        even though hooks must never gate the mutation they observe (T-323.1-31).

        The session shape does not change the context kind: ``ctx.context_kind``
        is ``"hook"`` here exactly as for every other hook, because the hook bus
        states it explicitly (``dispatch_hook``) instead of deriving it from the
        session. The Core-data write surface therefore stays closed — a
        subscriber deletes only its OWN tables through ``ctx.db()``; an SDK
        Core-data write raises the stable ``hook_context`` code.

        Payload: ``language_code`` (the deleted row's code) and ``actor_id`` (the
        authenticated actor that issued the delete). Not fired from a rejected
        delete (the bundled / referenced 409 branches).
        """
        return None

    async def before_index(self, ctx: Any, **kwargs: Any) -> None:
        return None

    async def after_index(self, ctx: Any, **kwargs: Any) -> None:
        return None


def capability_declaration_mismatch(plugin: IndraMeshPlugin) -> str | None:
    """D-02: cross-check a plugin's declared capabilities against its real classes.

    Returns ``None`` when the manifest's ``capabilities`` list equals the set of
    registered capability interfaces the instance actually implements, otherwise
    a descriptive string naming which side has the extra/missing entries.

    Deliberately generic over whatever interfaces happen to be registered at
    call time — no forward reference to any concrete capability class.
    """
    declared = set(plugin.manifest.capabilities) if plugin.manifest is not None else set()
    actual = {
        name
        for name, cls in IndraMeshPlugin._capability_interfaces.items()
        if isinstance(plugin, cls)
    }
    if declared == actual:
        return None

    parts: list[str] = []
    extra = sorted(declared - actual)
    missing = sorted(actual - declared)
    if extra:
        parts.append(f"declared but not implemented: {', '.join(extra)}")
    if missing:
        parts.append(f"implemented but not declared: {', '.join(missing)}")

    plugin_name = plugin.manifest.name if plugin.manifest is not None else "?"
    return f"capability declaration mismatch for plugin {plugin_name!r}: " + "; ".join(parts)
