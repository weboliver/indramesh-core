import logging
import uuid
from typing import List, Literal
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.redis import get_arq_pool
from app.models.export_definition import ExportDefinition
from app.models.import_definition import ImportDefinition
from app.models.import_export import Export
from app.models.object_type import ObjectType
from app.models.schedule import Schedule
from app.models.task import Task, TaskType, TaskStep, TaskStatus
from app.schemas.crud import (
    TaskCreate, TaskUpdate, TaskResponse, TaskActionResponse, ExportCreate, ImportCreate,
    TaskTypeCatalogEntry,
)
from app.schemas.auth import UserResponse
from app.schemas.task_log import TaskLogPage
from app.logging.security import mark_permission_denied
from app.services.auth_service import (
    get_current_user,
    require_permission,
    resolve_user_permissions,
)
from app.services.task_log_service import (
    read_task_logs,
    render_task_log_download,
)
from app.services.export_service import create_export, visible_export_ids_select
from app.services.import_service import create_import as create_import_svc
from app.services.task_type_catalog import build_task_type_catalog
from app.services.task_service import (
    create_task, list_tasks, get_task as get_task_svc,
    update_task as update_task_svc, delete_task as delete_task_svc,
    retry_task, cancel_task, enqueue_task_job, PluginTaskAdmissionError,
    PluginTaskInvalidError, PluginTaskPermissionError,
    CORE_TASKTYPE_VALUES, TERMINAL_TASK_STATUSES, user_admission_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


async def _task_is_visible(
    db: AsyncSession, task: Task, current_user: UserResponse
) -> bool:
    """Export-derived read scoping for one task (T-MGK-08).

    A superuser, a non-EXPORT task and a permanent schedule template are always
    visible. An EXPORT run is visible only when it carries an ``export_id`` whose
    Export row the caller created — a NULL ``export_id`` fails closed, exactly
    like the list predicate in ``task_service.list_tasks``.
    """
    if current_user.is_superuser:
        return True
    if task.tasktype != TaskType.EXPORT.value or task.is_permanent:
        return True
    if task.export_id is None:
        return False
    owner = await db.execute(
        select(Export.id).where(
            Export.id == task.export_id,
            Export.creator_id == current_user.id,
        )
    )
    return owner.scalar_one_or_none() is not None


async def _require_import_create(current_user: UserResponse) -> None:
    """Require ``imports:create`` on a task route that starts an import.

    ``POST /tasks/`` and ``POST /tasks/{id}/retry`` are gated by
    ``tasks:create``, which Editor holds. An IMPORT task created or retried
    through them starts the same worker job as ``POST /imports/csv/upload``, so
    the import capability is checked here too — ``tasks:create`` alone must not
    be a way around the Administrator-only default. Superusers bypass, and the
    403 detail matches ``require_permission`` exactly.
    """
    if current_user.is_superuser:
        return
    permission_name = "imports:create"
    if permission_name in await resolve_user_permissions(current_user.id):
        return
    mark_permission_denied(permission_name)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Not authorized. Missing permission: {permission_name}.",
    )


def _admission_http_exception(exc: PluginTaskAdmissionError) -> HTTPException:
    """The ONE create/retry mapper for plugin admission failures (D-08/D-09).

    403: the caller (or its missing context) failed the declaration's
    ``required_permission`` — evaluated before any runtime probe, so the status
    never leaks availability (T-289-04-04).
    422: unknown/invalid task type or invalid parameters.
    409: runtime unavailability (disabled/errored/not-installed plugin, stale
    worker version) or a declaration ``admit`` rejection — the reason is
    Core-authored and bounded.
    """
    if isinstance(exc, PluginTaskPermissionError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        )
    if isinstance(exc, PluginTaskInvalidError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    # PluginTaskUnavailableError and any bare base-class failure.
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task_route(
    task_data: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "create"))
):
    try:
        # CSV export path (D-01/D-02): auto-create Export + Task + enqueue
        if task_data.tasktype == "EXPORT" and task_data.export_definition_id:
            # Resolve ExportDefinition → ObjectType → schema_id via explicit JOIN
            # (avoids lazy-loading the object_type relationship)
            result = await db.execute(
                select(ExportDefinition, ObjectType.schema_id)
                .join(ObjectType, ObjectType.id == ExportDefinition.object_type_id)
                .where(ExportDefinition.id == task_data.export_definition_id)
            )
            row = result.one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail="ExportDefinition not found")
            _definition, schema_id = row

            # WR-03: target_id is required at ExportDefinition creation time, but the
            # FK is ondelete="SET NULL", so a previously-valid definition can drift to
            # target_id=None if its Target is deleted later. Surface that clearly here
            # instead of letting execute_csv_export fail deep in the worker with a
            # confusing "Target None not found..." message.
            if _definition.target_id is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"ExportDefinition {_definition.id} has no target configured "
                    "(its Target may have been deleted) — set target_id before running this export",
                )

            # Create Export record. The acting user owns it (server-authored
            # creator_id); object_key is not part of the request schema any more
            # and is written only by the worker once the CSV exists.
            export_create = ExportCreate(
                schema_id=schema_id,
                status="pending",
            )
            export_obj = await create_export(
                db, export_create, creator_id=current_user.id
            )

            # Create Task linked to Export
            task_create_data = TaskCreate(
                export_id=export_obj.id,
                current_step=TaskStep.SCHEMA.value,
                current_status=TaskStatus.PENDING.value,
                tasktype=TaskType.EXPORT.value,
                is_permanent=task_data.is_permanent,
            )
            task = await create_task(db, task_create_data, actor_id=current_user.id)

            # Enqueue ARQ job with export_definition_id
            try:
                arq_pool = await get_arq_pool()
                await arq_pool.enqueue_job(
                    "process_export",
                    str(task.id),
                    str(task_data.export_definition_id),
                    _job_id=f"export_{task.id}",
                )
            except Exception:
                logger.exception("failed to enqueue CSV export job for task %s", task.id)

            return task

        # IMPORT+definition path (mirrors EXPORT+definition path above)
        if task_data.tasktype == "IMPORT" and task_data.import_definition_id:
            # 261022: this branch enqueues the same import job as the
            # imports routes, so `tasks:create` alone must not reach it.
            await _require_import_create(current_user)
            # Resolve ImportDefinition → ObjectType → schema_id
            result = await db.execute(
                select(ImportDefinition, ObjectType.schema_id)
                .join(ObjectType, ObjectType.id == ImportDefinition.object_type_id)
                .where(ImportDefinition.id == task_data.import_definition_id)
            )
            row = result.one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail="ImportDefinition not found")
            _definition, schema_id = row

            # Create Import record
            import_create = ImportCreate(
                schema_id=schema_id,
                object_key=None,
                status="pending",
            )
            import_obj = await create_import_svc(db, import_create)

            # Create Task linked to Import with import_definition_id
            task_create_data = TaskCreate(
                import_id=import_obj.id,
                import_definition_id=task_data.import_definition_id,
                current_step=TaskStep.SCHEMA.value,
                current_status=TaskStatus.PENDING.value,
                tasktype=TaskType.IMPORT.value,
                is_permanent=task_data.is_permanent,
            )
            task = await create_task(db, task_create_data, actor_id=current_user.id)

            # Enqueue ARQ job with import_definition_id
            # NOTE: No temp_file_path — this path is for definition-based task creation
            # (e.g., from schedules). CSV uploads with files go through /imports/csv/upload.
            try:
                arq_pool = await get_arq_pool()
                await arq_pool.enqueue_job(
                    "process_import",
                    str(task.id),
                    import_definition_id=str(task_data.import_definition_id),
                    _job_id=f"import_{task.id}",
                )
            except Exception:
                logger.exception("failed to enqueue import job for task %s", task.id)

            return task

        # Plugin task path (Phase 286, PLUGTASK-04): a declared plugin type is
        # admitted by create_task's shared gate, then explicitly enqueued via the
        # one static generic wrapper (`run_plugin_task`). The standard branch
        # below never enqueues — the native IMPORT/EXPORT/reindex jobs are
        # enqueued by their own routes — so without this a plugin Task would sit
        # PENDING forever. create_task has already
        # committed the row, so an enqueue failure is deliberately NOT swallowed:
        # it raises a 5xx so the caller knows the committed PENDING row was never
        # queued (the row itself stays PENDING, same as the Core branches).
        if task_data.tasktype not in CORE_TASKTYPE_VALUES:
            # D-04/D-07: the plugin start path passes ONLY the authenticated
            # current-user context built from the route dependency — never a
            # context, mode or permission set taken from the request body or
            # an absent actor (T-289-04-02).
            task = await create_task(
                db, task_data, actor_id=current_user.id,
                admission_context=await user_admission_context(current_user),
            )
            arq_pool = await get_arq_pool()
            await enqueue_task_job(
                arq_pool, task_data.tasktype, str(task.id), task,
                _job_id=f"plugin_{task.id}",
            )
            return task

        # Standard task creation path
        return await create_task(db, task_data, actor_id=current_user.id)
    except PluginTaskAdmissionError as e:
        raise _admission_http_exception(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[TaskResponse])
async def list_tasks_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    # Phase 286 (PLUGTASK-01): task_type is a bounded string, not the closed
    # TaskType enum — a plugin-defined persisted value must filter instead of
    # raising 422. current_status stays enum-typed (statuses remain Core-only).
    task_type: str | None = Query(None, max_length=255, description="Filter by task type (Core value or plugin-qualified name)"),
    current_status: TaskStatus | None = Query(None, description="Filter by current status (PENDING, RUNNING, SUCCESS, PARTIAL_SUCCESS, FAILED, DEAD_LETTER)"),
    schedule_id_null: bool | None = Query(None, description="Filter to classes (True) or instances (False) by schedule_id"),
    is_permanent: bool | None = Query(None, description="Filter by is_permanent flag"),
    # 261022-np1: the Create Schedule template picker must offer only the
    # caller's own templates. Unlike the export-run scoping, this applies to a
    # superuser too (their own templates are still theirs); the schedule create
    # rule remains authoritative and still lets a superuser use any
    # non-IMPORT/EXPORT template by id.
    created_by_me: bool | None = Query(
        None, description="Only tasks whose server-authored creator is the caller"
    ),
    response: Response = None,
):
    # Phase 247 (T-247-07/T-247-08): an invalid status is rejected by
    # FastAPI/Pydantic validation (422) before ever reaching the server-side SQL
    # filter — never client-filtered after the fact. Phase 286 widens task_type
    # to the shared bounded string so plugin task types are filterable.
    items, total = await list_tasks(
        db, skip=skip, limit=limit,
        task_type=task_type,
        current_status=current_status.value if current_status else None,
        schedule_id_null=schedule_id_null, is_permanent=is_permanent,
        creator_id=current_user.id if created_by_me else None,
        # T-MGK-08: export runs are projected only inside the caller's own
        # exports; None (superuser) means no restriction.
        visible_export_ids=visible_export_ids_select(
            current_user.id, current_user.is_superuser
        ),
        permanent_owner_id=current_user.id,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/types", response_model=List[TaskTypeCatalogEntry])
async def list_task_types_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "read"))
):
    """Phase 289 (CORESEAM-03, D-04/D-05/D-10): the task-type catalog.

    Declared BEFORE ``/{task_id}`` so this literal path can never be parsed as
    a Task UUID. ``tasks:read`` is the only gate (D-05): plugin entries are
    additionally filtered by each declaration's ``required_permission`` (D-04),
    a stale worker keeps an ACTIVE entry visible with ``dispatchable=False``
    (D-10), and the response carries no raw exception, config or secret text.
    """
    return await build_task_type_catalog(db, current_user)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task_route(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "read"))
):
    task = await get_task_svc(db, task_id)
    if not task or not await _task_is_visible(db, task, current_user):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/{task_id}/logs", response_model=None)
async def get_task_logs_route(
    task_id: uuid.UUID,
    download: bool = Query(False, description="Return the plain-text .log snapshot"),
    after: int = Query(0, ge=0, description="Return lines with sequence greater than this"),
    limit: int = Query(200, ge=1, le=500, description="Maximum lines scanned per page"),
    min_level: Literal["INFO", "WARNING", "ERROR"] = Query(
        "INFO", description="Minimum level: INFO+, WARNING+ or ERROR+"
    ),
    response: Response = None,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "read")),
):
    """Read the stored task log (TLOG-03) or download it as plain text (D-16).

    ``tasks:read`` gates both the JSON page and the download (Carry-04); no new
    permission exists. The download ignores ``min_level`` on purpose and always
    returns the whole retained history.
    """
    response.headers["Cache-Control"] = "private, no-store"

    # T-MGK-08: the export-derived log surface is scoped before any log read, so
    # a foreign EXPORT run is indistinguishable from a missing one.
    task = await get_task_svc(db, task_id)
    if not task or not await _task_is_visible(db, task, current_user):
        raise HTTPException(status_code=404, detail="Task not found")

    if download:
        text = await render_task_log_download(db, task_id)
        if text is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return Response(
            content=text,
            media_type="text/plain; charset=utf-8",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="task-{task_id}.log"',
            },
        )

    page: TaskLogPage | None = await read_task_logs(
        db, task_id, after=after, limit=limit, min_level=min_level
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return page


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task_route(
    task_id: uuid.UUID,
    task_update: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "update"))
):
    # Phase 300 (T-300-05) + quick 261022-mgk (T-MGK-07): a non-terminal task's
    # completion belongs to the worker or to POST /tasks/{id}/cancel. PENDING is
    # covered as well as RUNNING: otherwise a client could declare a queued run
    # terminal and then delete its export past the active-task 409 rule.
    # Internal task_service.update_task callers are unaffected.
    existing = await get_task_svc(db, task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if existing.current_status not in TERMINAL_TASK_STATUSES:
        writes_terminal = (
            task_update.current_status is not None
            and task_update.current_status in TERMINAL_TASK_STATUSES
        )
        if (
            writes_terminal
            or task_update.completed_at is not None
            or task_update.current_step is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A non-terminal task's completion is owned by the worker",
            )
    try:
        task = await update_task_svc(db, task_id, task_update, actor_id=current_user.id)
    except ValueError as e:
        # Phase 286 (D-01/D-03): an identity-rewriting tasktype write is a
        # client error, not a server fault.
        raise HTTPException(status_code=422, detail=str(e))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_route(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "delete"))
):
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.is_permanent:
        schedule_result = await db.execute(
            select(Schedule.id, Schedule.name).where(
                Schedule.template_type == "task",
                Schedule.template_id == task_id,
            )
        )
        schedules = schedule_result.all()
        if schedules:
            names = ", ".join(f"'{s.name}' (id={s.id})" for s in schedules)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot delete Task {task_id}: it is marked as permanent "
                       f"and is referenced by schedule(s): {names}. Unlink the schedule(s) first.",
            )

    deleted = await delete_task_svc(db, task_id, actor_id=current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    return None


@router.post("/{task_id}/retry", response_model=TaskActionResponse, status_code=status.HTTP_201_CREATED)
async def retry_task_route(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "create"))
):
    try:
        # 261022: retrying an IMPORT task re-runs the import, so it needs the
        # import capability as well — `tasks:create` alone is not enough. The
        # type is read from the stored task; a missing task keeps retry's 404.
        existing = await get_task_svc(db, task_id)
        if existing and existing.tasktype == TaskType.IMPORT.value:
            await _require_import_create(current_user)
        # D-07: retry reauthorizes the CURRENT user — the original Task's prior
        # admission never authorizes this new start.
        new_task = await retry_task(
            db, task_id, actor_id=current_user.id,
            admission_context=await user_admission_context(current_user),
        )
        if not new_task:
            raise HTTPException(status_code=404, detail="Task not found")
        return TaskActionResponse(task=new_task, message="Task retry initiated. New job enqueued.")
    except PluginTaskAdmissionError as e:
        raise _admission_http_exception(e)
    except ValueError as e:
        # The pre-289 retry guard ("Can only retry failed tasks") stays a 409.
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task_route(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("tasks", "create"))
):
    try:
        task = await cancel_task(db, task_id, actor_id=current_user.id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
