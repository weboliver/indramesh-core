"""PluginContext (D-05/D-06/D-07) — the capability-agnostic service bundle.

The orchestration depth lives here rather than in ``ConnectorPlugin`` (D-05):
Phase 282 ships the core set — ``ctx.db()``, ``ctx.config``, ``ctx.logger`` and
``ctx.report_step(...)``. Checkpoint/Resume and Audit/Outbox helpers are added
additively only once a real consumer exists (D-07).

A ``PluginContext`` is built per plugin call by ``PluginRegistry.build_context``
and carries only that one plugin's own config namespace — isolation by
construction (PLUGCORE-06): there is no path to another plugin's config or any
core credential.

``ctx.config`` is the decrypted, schema-defaulted view of that one plugin's
``plugins.config`` row (D-12): ``format:password`` fields are decrypted with the
shared Fernet mechanism, and schema defaults apply at read time only. It is
memoized per context — a context is constructed fresh per call per plugin, so
there is intentionally no ``set_config``/mutator on this class.

CORESEAM-04 (Phase 289) adds the task-bound runtime capabilities a connector
task needs: ``ctx.task`` (the already-loaded projection of THIS call's Task —
there is deliberately no lookup API), ``ctx.set_outcome`` (an allowlisted
terminal success outcome the Core wrapper persists instead of its SUCCESS
default), ``ctx.report_progress`` (the existing monotonic, throttled Task
progress writer), ``ctx.credentials`` (only the ServiceAccount bound through
``Task.service_account_id``) and ``ctx.invalidate`` (runtime cache invalidation
requests, applied by the Core wrapper after a successful run).

Every one of those members is TASK-BOUND: a route/hook context built by
``build_context(name)`` — which carries no task — raises ``PluginTaskError``
instead of guessing an identity, while its existing surface
(``config``/``logger``/``db``/``report_step``) is unchanged.

Phase 302 (LLM-04) adds one deliberately NON-task-bound Core capability:
``ctx.llm`` (D-09/D-10) is the process-wide provider-neutral ``app.llm``
facade. It is available on route, task and hook contexts alike — the
schema-wizard route call carries no task — so it never applies the task-id
guard. It returns the facade even when no key is configured so a
plugin can ask ``ctx.llm.is_configured()``; a call without a key raises the
typed ``LLMNotConfigured``. A plugin only ever sees the Core facade, never the
raw ``instructor``/``openai`` client.
"""

import copy
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.llm import LLMClient, get_llm_client
from app.models.task import Task, TaskStatus
from app.plugin_system import crypto, validation
from app.plugin_system.capabilities import PluginTaskError
from app.schemas.crud import TaskUpdate
from app.services import task_service

# CORESEAM-04: the ONLY task statuses a plugin may select for a successful run.
# FAILED/DEAD_LETTER stay Core-owned (ARQ retry/dead-letter policy decides them)
# and PENDING/RUNNING are lifecycle states the Core worker writes.
ALLOWED_SUCCESS_OUTCOMES = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.PARTIAL_SUCCESS}
)

# The closed set of Core-set context kinds (CORESEAM-10). Anything else is a
# Core-side mistake and fails at construction instead of silently reaching the
# Core-data write resolver.
CONTEXT_KINDS: frozenset[str] = frozenset({"task", "route", "hook"})


class PluginContext:
    """Service bundle handed to a plugin capability call or hook."""

    def __init__(
        self,
        plugin_name: str,
        raw_config: dict,
        active_session: AsyncSession | None = None,
        task_id: uuid.UUID | None = None,
        config_schema: dict | None = None,
        step_sequence: tuple[str, ...] | None = None,
        task: Task | None = None,
        context_kind: str | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._raw_config = raw_config
        self._active_session = active_session
        # CORESEAM-04: the already-loaded Task of THIS call (the worker passes
        # it). Never looked up here — a plugin can only ever see its own row.
        self._task = task
        self._task_id = task.id if task_id is None and task is not None else task_id
        # CORESEAM-10 / D-19: the Core-set context kind the Core-data write
        # resolver dispatches on. Core states it explicitly whenever the shape
        # is not derivable from what it passes: the hook bus always passes
        # ``"hook"``, because a post-commit hook legitimately runs with
        # ``active_session=None`` (``on_language_deleted``) and deriving the
        # kind from session ownership would misclassify it as a route context.
        # Without an explicit kind it is derived from the call shape: a loaded
        # task is ``task``, a borrowed session (the in-transaction object hooks)
        # is ``hook``, and neither (route/lifecycle) is ``route``. Either way it
        # only selects the correct actor rule; it is NOT an anti-forgery control
        # (D-14).
        if context_kind is None:
            if task is not None:
                context_kind = "task"
            elif active_session is not None:
                context_kind = "hook"
            else:
                context_kind = "route"
        elif context_kind not in CONTEXT_KINDS:
            raise ValueError(
                f"Unknown context kind {context_kind!r} — expected one of "
                f"{sorted(CONTEXT_KINDS)}"
            )
        self._context_kind = context_kind
        self._config_schema = config_schema or {}
        self._resolved_config: dict | None = None
        # CORESEAM-04 runtime state, all reset with the context (= one attempt).
        self._selected_outcome: TaskStatus | None = None
        # Insertion-ordered set: first request wins, repeats are deduplicated.
        self._requested_invalidations: dict[str, None] = {}
        self._credentials: task_service.TaskCredentials | None = None
        # D-08/D-09: the owning declaration's ordered business steps. Empty means
        # the plugin may report arbitrary step names. ``_last_step_index`` is the
        # per-attempt monotonic baseline: one context is built per call (= one
        # attempt), so an ARQ CancelledError re-run gets a fresh baseline and may
        # legitimately report the first step again even though the persisted row
        # still carries attempt 1's later step.
        self._step_sequence = tuple(step_sequence) if step_sequence else ()
        self._last_step_index: int | None = None

    @property
    def config(self) -> dict:
        """This plugin's own decrypted, schema-defaulted config namespace (D-12).

        Secret fields (``format:password``) are decrypted from their stored
        ``v1:`` ciphertext; every other key passes through unchanged; finally any
        schema-declared default fills a key the plugin never configured. The
        result is memoized — config never changes within one ``PluginContext``'s
        lifetime.
        """
        if self._resolved_config is None:
            decrypted = copy.deepcopy(self._raw_config)
            for path in validation.secret_field_names(self._config_schema):
                present, value = crypto._get_path(decrypted, path)
                if present:
                    crypto._set_path(decrypted, path, crypto.decrypt_secret_field(value))
            self._resolved_config = validation.apply_schema_defaults(
                self._config_schema, decrypted
            )
        return self._resolved_config

    @property
    def logger(self) -> logging.Logger:
        """A logger namespaced to this plugin (``plugin.<name>``)."""
        return logging.getLogger(f"plugin.{self._plugin_name}")

    @property
    def llm(self) -> LLMClient:
        """The process-wide provider-neutral LLM facade (D-09/D-10).

        Deliberately NON-task-bound: a route context (the schema-wizard call
        carries no task), a task context and a hook context all get the same
        process-wide facade, so the task-id guard is never used here. It
        returns the Core ``app.llm`` facade unconditionally — even without a
        key — so a plugin can ask ``ctx.llm.is_configured()``; a call then
        raises the typed ``LLMNotConfigured``. Only Core-typed facade members
        are reachable: the raw vendor client and the API key are never exposed.
        Resolved through the module-level ``get_llm_client`` name, which is
        also the deterministic test seam.
        """
        return get_llm_client()

    @property
    def context_kind(self) -> str:
        """The Core-set context kind: ``"task"``, ``"route"`` or ``"hook"``.

        Read-only and set only by Core at construction (CORESEAM-10): the hook
        bus states ``"hook"`` explicitly, every other build lets the context
        derive it from the call shape. The Core-data write resolver uses it to
        pick the correct actor rule; it is not a security boundary (D-14).
        """
        return self._context_kind

    @asynccontextmanager
    async def db(self) -> AsyncIterator[AsyncSession]:
        """Yield a DB session (D-06).

        When the caller supplied an ``active_session`` (the hook path) it is
        yielded unchanged — the caller owns that transaction, so the context
        never commits or rolls it back. Otherwise a fresh session is opened via
        ``async_session_maker()`` and closed on exit (the route/connector path).
        """
        if self._active_session is not None:
            yield self._active_session
            return

        async with async_session_maker() as session:
            yield session

    def _require_task_id(self, capability: str) -> uuid.UUID:
        """The one task-bound guard for every CORESEAM-04 member (review 1).

        A route/hook context has no task: refusing loudly is the only safe
        answer — guessing an identity or silently no-op'ing would turn a plugin
        authoring mistake into either a cross-task effect or an invisible one.
        """
        if self._task_id is None:
            raise PluginTaskError(
                f"ctx.{capability} is only available to a task-bound plugin "
                "call — this context was built without a task"
            )
        return self._task_id

    @property
    def task(self) -> Task:
        """The already-loaded Task projection of THIS call (CORESEAM-04).

        Exposes exactly what the worker loaded before dispatch — never a query
        the plugin could steer, so there is no path to another task.
        """
        self._require_task_id("task")
        if self._task is None:
            raise PluginTaskError(
                "ctx.task is only available to a task-bound plugin call — this "
                "context carries no loaded task projection"
            )
        return self._task

    @property
    def selected_outcome(self) -> TaskStatus | None:
        """What ``ctx.set_outcome`` selected, or ``None`` (Core default SUCCESS)."""
        return self._selected_outcome

    @property
    def requested_invalidations(self) -> tuple[str, ...]:
        """Runtime invalidation requests in first-request order, deduplicated."""
        return tuple(self._requested_invalidations)

    def set_outcome(self, outcome: Any) -> None:
        """Select the terminal success status the Core wrapper persists.

        Synchronous on purpose: this only records the choice — the wrapper
        applies it AFTER ``run`` returns, so a plugin never writes a status
        itself. Accepts a ``TaskStatus`` member or its persisted string value,
        and only the Core success outcomes (SUCCESS / PARTIAL_SUCCESS); anything
        else raises ``PluginTaskError`` without echoing the rejected value
        (T-289-05-02).
        """
        self._require_task_id("set_outcome")
        candidate: TaskStatus | None
        if isinstance(outcome, TaskStatus):
            candidate = outcome
        elif isinstance(outcome, str):
            try:
                candidate = TaskStatus(outcome)
            except ValueError:
                candidate = None
        else:
            candidate = None
        if candidate not in ALLOWED_SUCCESS_OUTCOMES:
            raise PluginTaskError(
                "ctx.set_outcome() accepts only the Core success outcomes "
                "SUCCESS and PARTIAL_SUCCESS"
            )
        self._selected_outcome = candidate

    async def report_progress(
        self,
        processed_objects: int | None = None,
        total_objects: int | None = None,
        force: bool = False,
    ) -> Task | None:
        """Report server-observed progress through the existing throttled writer.

        Delegates to ``task_service.update_task_progress`` (Phase 247, R-02) with
        its own ``async_session_maker()`` session — that writer commits itself,
        so it must never share the caller's transaction (the same D-06 rationale
        as ``report_step``). Independent of any declared step sequence: a
        step-less declaration reports free-form counts exactly like a stepped
        one, and ``force=True`` keeps the writer's exact-monotonic-pair bypass.
        """
        task_id = self._require_task_id("report_progress")
        async with async_session_maker() as session:
            return await task_service.update_task_progress(
                session, task_id, processed_objects, total_objects, force
            )

    async def credentials(self) -> task_service.TaskCredentials:
        """The ServiceAccount credentials bound to THIS task (CORESEAM-04).

        Resolves only ``Task.service_account_id`` — there is deliberately no
        account-id parameter, so a plugin can never select another account
        (T-289-05-01). A missing account fails with a Core-authored safe message
        that never contains a decrypted value. Memoized per context.
        """
        task_id = self._require_task_id("credentials")
        if self._credentials is None:
            async with async_session_maker() as session:
                try:
                    self._credentials = await task_service.resolve_task_credentials(
                        session, task_id
                    )
                except ValueError as exc:
                    raise PluginTaskError(str(exc)) from None
        return self._credentials

    def invalidate(self, *entity_types: str) -> None:
        """Request post-run cache invalidation for Core cache entity types.

        Synchronous collection: the Core wrapper applies the deduplicated set
        after a successful ``run`` returns, so a plugin never mutates the cache
        mid-flight and a cache fault can never fail its task (fail-open).

        Every key must be a Core cache entity type (review 3) — the same fixed
        vocabulary the declaration's ``invalidates`` is validated against — so a
        typo fails the run instead of silently invalidating nothing.
        """
        self._require_task_id("invalidate")
        from app.plugin_system.task_registry import known_cache_entity_types

        known = known_cache_entity_types()
        for entity_type in entity_types:
            if not isinstance(entity_type, str) or entity_type not in known:
                raise PluginTaskError(
                    "ctx.invalidate() accepts only Core cache entity types — "
                    f"{entity_type!r} is not one of them"
                )
            self._requested_invalidations.setdefault(entity_type, None)

    async def report_step(self, step: str, **kwargs: Any) -> None:
        """Report Task progress by writing ``Task.current_step`` (D-07).

        Always uses its own ``async_session_maker()`` session — never
        ``self.db()``/the shared ``active_session``: ``task_service.update_task``
        commits itself, and committing the caller's live hook transaction
        mid-mutation would break D-06's "hook shares the running transaction"
        contract.

        Note for plugin authors: ``Task.current_step`` is a bounded ``String``
        and ``TaskUpdate`` enforces its own ``max_length`` — a longer step string
        raises a Pydantic ``ValidationError``. Declared steps are bounded to
        ``task_registry.STEP_MAX_LENGTH`` at registration time, so only an
        undeclared (free-form) step can hit that limit here.

        D-08/D-09: when the owning declaration supplies a step sequence, ``step``
        must be a declared member and its position must not move backward within
        this attempt; declared steps may be skipped (forward jumps are allowed).
        Without a sequence any non-empty string is accepted. A violation raises
        ``ValueError`` inside the plugin call, which the generic wrapper's
        safe-error path classifies (D-13).
        """
        if self._task_id is None:
            self.logger.warning(
                "report_step(%r) ignored — no task_id on this PluginContext", step
            )
            return

        if self._step_sequence:
            try:
                index = self._step_sequence.index(step)
            except ValueError:
                raise ValueError(
                    f"step {step!r} is not one of the declared steps "
                    f"{self._step_sequence!r}"
                ) from None
            if self._last_step_index is not None and index < self._last_step_index:
                raise ValueError(
                    f"step {step!r} moves backward from the previously reported "
                    f"step {self._step_sequence[self._last_step_index]!r}"
                )
            self._last_step_index = index
        elif not step:
            raise ValueError("step must be a non-empty string")

        async with async_session_maker() as session:
            await task_service.update_task(
                session, self._task_id, TaskUpdate(current_step=step)
            )
