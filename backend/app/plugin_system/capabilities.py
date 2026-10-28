"""Capability interface classes (D-08) — the plugin-author capability surface.

Each class declares itself with the explicit ``capability=`` class kwarg from
Plan 282-01 (D-02). That kwarg — not the bare subclass relationship — is what
registers a class in ``IndraMeshPlugin._capability_interfaces``, so a new
capability type is addable by defining one new class here (or anywhere else)
with zero registry/loader change (PLUGCORE-02).

Deliberately plain ``async def`` stubs with no ``abstractmethod``/decorators,
matching this codebase's "no ABC hierarchy" bias (``connectors/base.py``).
The capability NAMES below are the exact strings a manifest's ``capabilities``
list carries.
"""

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Protocol

from fastapi import APIRouter

from app.plugin_system.base import IndraMeshPlugin
from app.plugin_system.connector_registry import ConnectorTypeDeclaration


class ConnectorSession(Protocol):
    """Opaque, plugin-defined connector session handle.

    The core never inspects it — ``ConnectorPlugin.connect()`` returns whatever
    the plugin's own implementation needs to carry between ``push``/``pull``
    calls (D-08).
    """


class SearchProviderPlugin(IndraMeshPlugin, capability="search_provider"):
    """Provides search results for the core search surface."""

    async def search(self, ctx: Any, query: str, **kwargs: Any) -> list[dict]:
        return []


class ConnectorPlugin(IndraMeshPlugin, capability="connector"):
    """The D-08 SPEC-trio, with the orchestration depth in ``PluginContext``.

    CORESEAM-01: a ``ConnectorPlugin`` also declares the connector identities it
    contributes. A concrete subclass overrides :meth:`connector_types` to return
    its immutable
    :class:`~app.plugin_system.connector_registry.ConnectorTypeDeclaration`
    records; ``PluginRegistry`` activation publishes them for exactly as long as
    the plugin is ACTIVE and removes them on isolation/kill-switch/unload, so an
    inactive owner can never accept a new write. Declaring zero connector types
    is a plugin-authoring error and is rejected at registration — the
    declaration is the whole contract (mirrors ``TaskTypePlugin``).
    """

    def connector_types(self) -> list[ConnectorTypeDeclaration]:
        return []

    async def connect(self, ctx: Any) -> ConnectorSession:
        return None

    async def push(
        self, ctx: Any, session: ConnectorSession, payload: dict
    ) -> None:
        return None

    async def pull(
        self, ctx: Any, session: ConnectorSession, params: dict
    ) -> AsyncIterator[dict]:
        for item in ():
            yield item


class GraphExtensionPlugin(IndraMeshPlugin, capability="graph_extension"):
    """Contributes to the object reference graph surface."""

    async def extend_graph(self, ctx: Any, **kwargs: Any) -> dict:
        return {}


class PluginTaskError(Exception):
    """An explicitly safe, short business message a plugin may expose (D-13).

    The Core generic worker wrapper persists ``str(exc)`` verbatim for THIS
    exception type only. Every other exception — and any raw text or traceback
    it carries — is logged and replaced by a Core generic safe message instead,
    so a Task record never leaks implementation detail.
    """


class TaskAdmissionRejected(Exception):
    """A declaration's ``admit`` hook refusing a start with a safe reason.

    The Core admission gate maps THIS exception to a bounded ``409 Conflict``
    detail (D-08: it names the plugin's own short business reason only). Any
    other exception raised by ``admit`` is logged and replaced by a generic
    safe 409 message, so a raw traceback never reaches a client. Exported to
    plugin authors through the SDK facade (Plan 289-05).
    """


@dataclass(frozen=True)
class TaskTypeDeclaration:
    """One immutable plugin-provided Task type (PLUGTASK-02, CORESEAM-02).

    ``name`` is the complete, externally visible API identity and must be
    ``<plugin-name>.<task-name>`` with a lowercase kebab-case task segment. It
    is never derived, prefixed or aliased (D-03). ``run`` is the owning async
    runner, invoked as ``run(ctx, task_id, **validated_parameters)``.
    ``steps`` is the optional ordered business-step sequence (D-08); an empty
    tuple means the plugin may report arbitrary step names. ``parameter_schema``
    is the plugin-owned JSON Schema the Task API validates start parameters
    against before persistence (D-04); it must never mark a secret field (D-05).

    The CORESEAM-02 catalog/admission metadata (all optional, so a declaration
    written before Phase 289 keeps its exact meaning):

    ``label``
        Optional non-blank display label, returned verbatim by the task-type
        catalog. ``None`` falls back to the exact literal name.
    ``schedulable``
        Whether the Core Schedule API may target this type (D-06/D-07).
    ``scope_requirements``
        Task/Schedule scope columns a start must carry (subset of
        ``source_id``/``schema_id``/``object_type_id``/``target_id``).
    ``required_permission``
        An existing Core ``<resource>:<action>`` permission the current user
        must hold in addition to ``tasks:create`` (D-04/D-07); unknown names are
        rejected at registration.
    ``admission_permissions``
        An additive tuple of further existing Core ``<resource>:<action>``
        permissions the current user must hold — alongside ``tasks:create`` and
        ``required_permission`` — when starting this type through an
        authenticated user context (D-13/D-15). Every name is validated at
        registration exactly like ``required_permission`` (a Core permission or
        one this plugin declared; unknown/foreign names are rejected). The
        default empty tuple leaves an existing declaration's admission
        behaviour completely unchanged; the trusted scheduled context keeps
        skipping these lookups exactly as it skips ``required_permission``.
    ``admit``
        Optional async ``admit(session, task_data)`` single-flight/business
        admission hook, run inside the central pre-persistence gate. It may
        raise :class:`TaskAdmissionRejected` with a bounded safe reason.
    ``invalidates``
        Cache entity types to invalidate after a successful run.
    ``catalog_visible``
        Whether this type appears in the generic Create Task dialog's type
        choices (default true). ``False`` removes it from THAT surface only —
        it is never an authorization check: the task list, list type filter,
        direct start/retry and scheduling stay complete for a hidden type
        (SEAM-05, Carry-03). A missing field (an older catalogue) stays
        visible, so the default is backward compatible.
    """

    name: str
    run: Callable[..., Any]
    steps: tuple[str, ...] = ()
    parameter_schema: dict = field(default_factory=dict)
    label: str | None = None
    schedulable: bool = False
    scope_requirements: tuple[str, ...] = ()
    required_permission: str | None = None
    admission_permissions: tuple[str, ...] = ()
    admit: Callable[..., Any] | None = None
    invalidates: tuple[str, ...] = ()
    catalog_visible: bool = True


class TaskTypePlugin(IndraMeshPlugin, capability="task_type"):
    """Declares one or more plugin-provided Task types (PLUGTASK-02).

    A concrete plugin overrides :meth:`task_types` to return its immutable
    :class:`TaskTypeDeclaration` records. A ``TaskTypePlugin`` that declares zero
    types is rejected by registry construction — the declaration is the whole
    contract, so a silent empty capability is a plugin-authoring error.
    """

    def task_types(self) -> list[TaskTypeDeclaration]:
        return []

    async def run(self, ctx: Any, task_id: Any, **kwargs: Any) -> dict:
        return {}


class ApiRoutesPlugin(IndraMeshPlugin, capability="api_routes"):
    """Registers plugin-owned API routes.

    Synchronous on purpose: building a router is not I/O. The core supplies the
    ``/api/v1/plugins/<name>/`` prefix and the forced auth dependency chain
    (D-16/PLUGCORE-09).
    """

    def register_routes(self, ctx: Any) -> APIRouter:
        return APIRouter()
