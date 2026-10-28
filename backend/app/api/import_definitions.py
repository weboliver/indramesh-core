import logging
import os
import uuid
from typing import List
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.redis import get_arq_pool
from app.models import ObjectType, ObjectTypeAttribute, ImportDefinition, Schedule, Source
from app.models.task import TaskType, TaskStep, TaskStatus
from app.schemas.crud import (
    ImportDefinitionCreate, ImportDefinitionUpdate, ImportDefinitionResponse,
    TaskCreate, TaskResponse, ImportCreate,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.import_service import create_import
from app.services.task_service import create_task
from app.services.audit_service import audit_log, model_to_dict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/import-definitions", tags=["import-definitions"])

# Shared with backend/app/worker.py's process_folder_import — both must resolve the
# same containment root, or a file that passes the API-side check here could still
# be rejected (or worse, a differently-configured worker could escape it) later.
IMPORT_ROOT = os.path.realpath(os.getenv("IMPORT_ROOT", "/tmp/imports"))


@router.post("/", response_model=ImportDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def create_import_definition(
    definition: ImportDefinitionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("import-definitions", "create"))
):
    result = await db.execute(
        select(ObjectType).where(ObjectType.id == definition.object_type_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="ObjectType not found")

    result = await db.execute(
        select(Source).where(Source.id == definition.source_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Source not found")

    if definition.identity_attr_id is not None:
        result = await db.execute(
            select(ObjectTypeAttribute).where(ObjectTypeAttribute.id == definition.identity_attr_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="ObjectTypeAttribute not found")

    # mode="json" — FieldMappingItem.attr_id (uuid.UUID) and any datetime fields must be
    # serialized to JSON-safe strings before hitting the plain JSON column; model_dump()
    # default (mode="python") leaves them as raw objects and json.dumps raises TypeError
    # at INSERT time.
    db_def = ImportDefinition(**definition.model_dump(mode="json"))
    db.add(db_def)
    await db.flush()
    new_values = model_to_dict(db_def)
    await audit_log(db, "import_definitions", db_def.id, "CREATE", current_user.id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_def)
    return db_def


@router.get("/", response_model=List[ImportDefinitionResponse])
async def list_import_definitions(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("import-definitions", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    response: Response = None,
):
    count_result = await db.execute(
        select(func.count()).select_from(ImportDefinition)
    )
    total = count_result.scalar()

    result = await db.execute(
        select(ImportDefinition).offset(skip).limit(limit).order_by(ImportDefinition.name)
    )
    items = result.scalars().all()

    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{definition_id}", response_model=ImportDefinitionResponse)
async def get_import_definition(
    definition_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("import-definitions", "read"))
):
    result = await db.execute(
        select(ImportDefinition).where(ImportDefinition.id == definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail="Import definition not found")
    return definition


@router.patch("/{definition_id}", response_model=ImportDefinitionResponse)
async def update_import_definition(
    definition_id: uuid.UUID,
    definition_update: ImportDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("import-definitions", "update"))
):
    result = await db.execute(
        select(ImportDefinition).where(ImportDefinition.id == definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail="Import definition not found")

    old_values = model_to_dict(definition)
    if definition_update.object_type_id is not None:
        result = await db.execute(
            select(ObjectType).where(ObjectType.id == definition_update.object_type_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="ObjectType not found")

    if definition_update.source_id is not None:
        result = await db.execute(
            select(Source).where(Source.id == definition_update.source_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Source not found")

    if definition_update.identity_attr_id is not None:
        result = await db.execute(
            select(ObjectTypeAttribute).where(ObjectTypeAttribute.id == definition_update.identity_attr_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="ObjectTypeAttribute not found")

    # See create_import_definition — mode="json" ensures UUID/datetime fields are
    # JSON-serializable before touching the JSON column.
    update_data = definition_update.model_dump(mode="json", exclude_unset=True)
    for field, value in update_data.items():
        setattr(definition, field, value)

    new_values = model_to_dict(definition)
    await audit_log(db, "import_definitions", definition_id, "UPDATE", current_user.id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(definition)
    return definition


@router.post("/{definition_id}/import-from-folder", response_model=List[TaskResponse], status_code=status.HTTP_202_ACCEPTED)
async def import_from_folder(
    definition_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "create"))
):
    """Scan the ImportDefinition's Source.import_folder for CSV files and enqueue one
    Import+Task per file found — the folder-based counterpart to POST /imports/csv/upload.

    Each file is processed by worker.process_folder_import, which copies it to a temp
    path (so the existing execute_csv_import can safely unlink its own copy) and, on
    success, moves the ORIGINAL out of the way into <folder>/processed/ so a later scan
    never reprocesses it. A failed file is left in place for the next scan/retry.
    """
    result = await db.execute(
        select(ImportDefinition, ObjectType.schema_id, Source)
        .join(ObjectType, ObjectType.id == ImportDefinition.object_type_id)
        .join(Source, Source.id == ImportDefinition.source_id)
        .where(ImportDefinition.id == definition_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="ImportDefinition not found")
    definition, schema_id, source = row

    if not source.import_folder:
        raise HTTPException(
            status_code=422,
            detail=f"Source {source.id} has no import_folder configured",
        )

    folder_path = os.path.join(IMPORT_ROOT, source.import_folder)
    resolved_folder = os.path.realpath(folder_path)
    if resolved_folder != IMPORT_ROOT and not resolved_folder.startswith(IMPORT_ROOT + os.sep):
        raise HTTPException(
            status_code=422,
            detail=f"Source import_folder '{source.import_folder}' resolves outside the "
            f"permitted import root ({IMPORT_ROOT})",
        )

    # Auto-create the folder on first use — mirrors the os.makedirs(exist_ok=True)
    # pattern already used for upload/processed/export directories elsewhere in this
    # codebase (imports.py, worker.py, export_execution_service.py). Without this, a
    # freshly-configured Source.import_folder that a user has never dropped a file
    # into yet 404s here, and there's no way for a user to create the directory
    # themselves inside the Docker volume without shelling into the container.
    os.makedirs(resolved_folder, exist_ok=True)

    csv_files = sorted(
        f for f in os.listdir(resolved_folder)
        if f.lower().endswith(".csv") and os.path.isfile(os.path.join(resolved_folder, f))
    )
    if not csv_files:
        raise HTTPException(status_code=404, detail=f"No CSV files found in {resolved_folder}")

    tasks = []
    for filename in csv_files:
        import_create = ImportCreate(schema_id=schema_id, object_key=None, status="pending")
        import_obj = await create_import(db, import_create)

        task_create_data = TaskCreate(
            import_id=import_obj.id,
            import_definition_id=definition_id,
            current_step=TaskStep.SCHEMA.value,
            current_status=TaskStatus.PENDING.value,
            tasktype=TaskType.IMPORT.value,
        )
        task = await create_task(db, task_create_data, actor_id=current_user.id)

        try:
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job(
                "process_folder_import",
                str(task.id),
                import_definition_id=str(definition_id),
                source_file_path=os.path.join(resolved_folder, filename),
                _job_id=f"folder_import_{task.id}",
            )
        except Exception:
            logger.exception("failed to enqueue folder import job for task %s", task.id)

        tasks.append(task)

    return tasks


class FolderFileUploadResult(BaseModel):
    """One accepted file of a folder-import upload — mirrors the per-file result the
    CSV upload path reports through its Task, without creating a Task here."""

    filename: str
    size_bytes: int


def _resolve_import_folder(import_folder: str) -> str:
    """Resolve `<IMPORT_ROOT>/<import_folder>` with the scan route's containment check.

    realpath (not normpath) so a symlinked import folder cannot point the writer
    outside the shared volume the backend and the worker both mount. Raises 422,
    exactly like `import_from_folder` does for the same misconfiguration.
    """
    resolved_folder = os.path.realpath(os.path.join(IMPORT_ROOT, import_folder))
    if resolved_folder != IMPORT_ROOT and not resolved_folder.startswith(IMPORT_ROOT + os.sep):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Source import_folder '{import_folder}' resolves outside the "
            f"permitted import root ({IMPORT_ROOT})",
        )
    return resolved_folder


def _validate_folder_upload_name(raw_name: str | None) -> str:
    """Return the filename only when it is a plain `.csv` file name, never a path.

    Two independent guards: a name that carries any directory component is refused
    outright, and the caller still resolves the destination with realpath and checks
    it against the folder (defence in depth for `.` segments and odd unicode).
    """
    name = raw_name or ""
    if not name.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only .csv files are accepted",
        )
    if "\x00" in name or name in (".", "..") or os.path.basename(name) != name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file name '{name}': the import folder accepts plain file names only",
        )
    return name


@router.post(
    "/{definition_id}/folder-files",
    response_model=List[FolderFileUploadResult],
    status_code=status.HTTP_201_CREATED,
)
async def upload_folder_files(
    definition_id: uuid.UUID,
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("imports", "create"))
):
    """Write uploaded CSV files into `IMPORT_ROOT/<Source.import_folder>/`.

    The counterpart `POST /{definition_id}/import-from-folder` only scans a directory
    — without this route nothing in the API can put a file there, so the folder
    trigger could never run on the chart-deployed stack (CR314-23, report 07 T5).
    The scan itself is untouched: it lists the same directory and creates one
    Import+Task per file.

    Each body is streamed to disk in 1 MiB chunks (never buffered whole) with the
    same `IMPORT_MAX_FILE_SIZE` ceiling the CSV upload applies, written to a `.part`
    sibling and renamed into place so a concurrent scan never reads a half file.
    Every name is validated before anything is written: a non-CSV file is 422 and a
    name whose resolved destination leaves the import root is 400 with nothing stored.
    """
    result = await db.execute(
        select(ImportDefinition, Source)
        .join(Source, Source.id == ImportDefinition.source_id)
        .where(ImportDefinition.id == definition_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="ImportDefinition not found")
    definition, source = row

    if not source.import_folder:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Source {source.id} has no import_folder configured",
        )

    resolved_folder = _resolve_import_folder(source.import_folder)

    # Validate every destination before the first byte is written — a rejected
    # request must not leave a partial set of files behind for the next scan.
    destinations = []
    for upload in files:
        name = _validate_folder_upload_name(upload.filename)
        destination = os.path.join(resolved_folder, name)
        resolved_destination = os.path.realpath(destination)
        if (
            resolved_destination != resolved_folder
            and not resolved_destination.startswith(resolved_folder + os.sep)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File name '{name}' resolves outside the import folder",
            )
        destinations.append((name, resolved_destination))

    os.makedirs(resolved_folder, exist_ok=True)

    max_size = int(os.getenv("IMPORT_MAX_FILE_SIZE", str(50 * 1024 * 1024)))
    chunk_size = 1024 * 1024
    results: List[FolderFileUploadResult] = []
    for upload, (name, destination) in zip(files, destinations):
        partial_path = destination + ".part"
        total_size = 0
        try:
            with open(partial_path, "wb") as sink:
                while True:
                    chunk = await upload.read(chunk_size)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > max_size:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"File exceeds maximum size of {max_size} bytes",
                        )
                    sink.write(chunk)
            os.replace(partial_path, destination)
        except HTTPException:
            if os.path.exists(partial_path):
                os.unlink(partial_path)
            raise
        except OSError as e:
            if os.path.exists(partial_path):
                os.unlink(partial_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to store uploaded file: {str(e)}",
            )
        results.append(FolderFileUploadResult(filename=name, size_bytes=total_size))

    return results


@router.delete("/{definition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import_definition(
    definition_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("import-definitions", "delete"))
):
    result = await db.execute(
        select(ImportDefinition).where(ImportDefinition.id == definition_id)
    )
    definition = result.scalar_one_or_none()
    if not definition:
        raise HTTPException(status_code=404, detail="Import definition not found")

    if definition.is_permanent:
        schedule_result = await db.execute(
            select(Schedule.id, Schedule.name).where(
                Schedule.template_type == "import",
                Schedule.template_id == definition_id,
            )
        )
        schedules = schedule_result.all()
        if schedules:
            names = ", ".join(f"'{s.name}' (id={s.id})" for s in schedules)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot delete ImportDefinition '{definition.name}': it is marked as permanent "
                       f"and is referenced by schedule(s): {names}. Unlink the schedule(s) first.",
            )

    old_values = model_to_dict(definition)
    await audit_log(db, "import_definitions", definition_id, "DELETE", current_user.id,
                    old_values=old_values, new_values=None)
    await db.delete(definition)
    await db.commit()
    return None
