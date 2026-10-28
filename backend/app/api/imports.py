import logging
import os
import tempfile
import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.redis import get_arq_pool
from app.models.import_definition import ImportDefinition
from app.models.object_type import ObjectType
from app.models.task import TaskType, TaskStep, TaskStatus
from app.schemas.crud import (
    ImportCreate, ImportUpdate, ImportResponse, TaskCreate, TaskResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.import_service import (
    create_import, list_imports, get_import as get_import_svc,
    update_import as update_import_svc, delete_import as delete_import_svc
)
from app.services.task_service import create_task as create_task_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("/", response_model=ImportResponse, status_code=status.HTTP_201_CREATED)
async def create_import_route(
    import_data: ImportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "create"))
):
    # DECOUPLE-05: definition-only dispatch contract. Without a definition this route
    # used to persist the Import, create the Task and enqueue process_import, whose
    # worker branch then silently performed a legacy Jira sync-pull. Reject before any
    # Import/Task write or enqueue so that hidden pull can never be reached.
    if not import_data.import_definition_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="import_definition_id is required",
        )

    # Validate the definition before any side effect (T-290-11-01) and resolve its
    # schema — the definition is the authority for the import's schema, mirroring
    # /imports/csv/upload and POST /tasks.
    definition_result = await db.execute(
        select(ImportDefinition, ObjectType.schema_id)
        .join(ObjectType, ObjectType.id == ImportDefinition.object_type_id)
        .where(ImportDefinition.id == import_data.import_definition_id)
    )
    definition_row = definition_result.one_or_none()
    if not definition_row:
        raise HTTPException(status_code=404, detail="ImportDefinition not found")
    _, schema_id = definition_row

    try:
        import_create = import_data.model_copy(update={"schema_id": schema_id})
        import_obj = await create_import(db, import_create)

        task_data = TaskCreate(
            import_id=import_obj.id,
            import_definition_id=import_data.import_definition_id,
            current_step=TaskStep.SCHEMA.value,
            current_status=TaskStatus.PENDING.value,
            tasktype=TaskType.IMPORT.value,
        )
        task = await create_task_svc(db, task_data, actor_id=current_user.id)

        try:
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job(
                "process_import",
                str(task.id),
                import_definition_id=str(import_data.import_definition_id),
                _job_id=f"import_{task.id}",
            )
        except Exception:
            logger.exception("failed to enqueue import job for task %s", task.id)

        return import_obj
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[ImportResponse])
async def list_imports_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_imports(db, skip=skip, limit=limit)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{import_id}", response_model=ImportResponse)
async def get_import_route(
    import_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "read"))
):
    import_obj = await get_import_svc(db, import_id)
    if not import_obj:
        raise HTTPException(status_code=404, detail="Import not found")
    return import_obj


@router.patch("/{import_id}", response_model=ImportResponse)
async def update_import_route(
    import_id: uuid.UUID,
    import_update: ImportUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "update"))
):
    import_obj = await update_import_svc(db, import_id, import_update)
    if not import_obj:
        raise HTTPException(status_code=404, detail="Import not found")
    return import_obj


@router.delete("/{import_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import_route(
    import_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "delete"))
):
    deleted = await delete_import_svc(db, import_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Import not found")
    return None


@router.post("/csv/upload", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_csv_for_import(
    file: UploadFile = File(...),
    import_definition_id: uuid.UUID = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "create"))
):
    """Upload a CSV file and create the Import + Task that process it.

    Validates file extension and size, writes to temp file, then creates the Import
    and Task rows and enqueues the ARQ job — mirroring how POST /api/v1/tasks creates
    Export+Task for CSV export (backend/app/api/tasks.py), so that a plain
    `POST /imports` (which auto-enqueues a legacy Jira sync-pull job) is never invoked
    as part of this flow. First UploadFile/multipart endpoint in the codebase (D-03).

    NOTE: Import creation and Task creation are two separate commits, not a single
    DB transaction (matching the same non-atomic pattern in tasks.py's EXPORT
    branch) — if Task creation fails after the Import has already committed, this
    endpoint compensates by deleting the orphaned Import row rather than leaving it
    stuck at status="pending" forever with no Task to process it.
    """
    # Validate csv extension (D-04) — cheap check before touching file content at all
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only .csv files are accepted",
        )

    # Verify import_definition_id is valid and resolve schema_id (D-06)
    # Join through ObjectType to get schema_id, same pattern as tasks.py's EXPORT branch —
    # ImportDefinition has no schema_id of its own (D-01: no Target reference).
    def_result = await db.execute(
        select(ImportDefinition, ObjectType.schema_id)
        .join(ObjectType, ObjectType.id == ImportDefinition.object_type_id)
        .where(ImportDefinition.id == import_definition_id)
    )
    row = def_result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="ImportDefinition not found")
    definition, schema_id = row

    # Validate max file size (D-04) — default 50MB. Stream-read in chunks and reject as soon as
    # the limit is exceeded instead of buffering the whole upload into memory first: reading the
    # full body before checking size defeats the DoS mitigation the size check exists for.
    max_size = int(os.getenv("IMPORT_MAX_FILE_SIZE", str(50 * 1024 * 1024)))
    chunk_size = 1024 * 1024
    total_size = 0
    chunks = []
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds maximum size of {max_size} bytes",
            )
        chunks.append(chunk)
    contents = b"".join(chunks)

    # Write to temp file (D-03) — must land in a directory shared between the
    # backend and worker containers (see docker-compose.yml `import_uploads`
    # volume mounted at IMPORT_UPLOAD_DIR in both services), since the worker
    # process that later opens this path runs in a separate container whose
    # default tempfile.gettempdir() is NOT shared with the backend's.
    upload_dir = os.getenv("IMPORT_UPLOAD_DIR")
    try:
        if upload_dir:
            os.makedirs(upload_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".csv", prefix="import_", dir=upload_dir or None
        ) as tmp:
            tmp.write(contents)
            temp_path = tmp.name
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to store uploaded file: {str(e)}",
        )
    del contents

    # Create Import + Task. create_import()/create_task_svc() each commit in their
    # own transaction (not a single atomic transaction — see WR-02), so if Task
    # creation fails after the Import row is already committed, compensate by
    # deleting the orphaned Import rather than leaving it stuck at status="pending"
    # forever with no Task to process it.
    import_create = ImportCreate(schema_id=schema_id, object_key=None, status="pending")
    import_obj = await create_import(db, import_create)

    task_create_data = TaskCreate(
        import_id=import_obj.id,
        current_step=TaskStep.SCHEMA.value,
        current_status=TaskStatus.PENDING.value,
        tasktype=TaskType.IMPORT.value,
    )
    try:
        task = await create_task_svc(db, task_create_data, actor_id=current_user.id)
    except Exception as e:
        try:
            await delete_import_svc(db, import_obj.id)
        except Exception:
            pass
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create import task: {str(e)}",
        )

    # Enqueue ARQ job using the Task's id — NOT the Import's id (process_import resolves
    # the Import via task.import_id, see backend/app/worker.py:192-215)
    try:
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "process_import",
            str(task.id),
            import_definition_id=str(import_definition_id),
            temp_file_path=temp_path,
            _job_id=f"csv_import_{task.id}",
        )
    except Exception as e:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enqueue import job: {str(e)}",
        )

    return task
