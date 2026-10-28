from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.storage_service.paths import (
    assert_contained,
    resolve_shard_path,
    validate_bucket,
    validate_key,
)
from app.storage_service.settings import settings
from app.storage_service.storage import (
    delete_blob,
    iterfile_from_open_file,
    write_blob_atomic,
)

router = APIRouter()


def _reject_invalid_request(bucket: str, key: str) -> None:
    """Validate bucket (404) then key (400) BEFORE any filesystem path is built (T-278-01-01)."""
    try:
        validate_bucket(bucket, settings.storage_bucket)
    except ValueError:
        raise HTTPException(status_code=404, detail="bucket not found")
    try:
        validate_key(key)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid key")


def _final_path(bucket: str, key: str) -> str:
    path = resolve_shard_path(settings.storage_blob_root, bucket, key)
    assert_contained(path, settings.storage_blob_root)
    return path


@router.put("/buckets/{bucket}/keys/{key}")
async def store_blob(bucket: str, key: str, request: Request) -> Response:
    _reject_invalid_request(bucket, key)
    final_path = _final_path(bucket, key)
    total = await write_blob_atomic(final_path, request.stream())
    return Response(status_code=204, headers={"X-Stored-Bytes": str(total)})


@router.get("/buckets/{bucket}/keys/{key}")
async def fetch_blob(bucket: str, key: str) -> StreamingResponse:
    _reject_invalid_request(bucket, key)
    final_path = _final_path(bucket, key)
    # Open eagerly (not a prior isfile() check) so a blob removed between
    # validation and open raises FileNotFoundError before any response
    # headers are sent — no TOCTOU window producing a truncated 200 stream.
    try:
        f = open(final_path, "rb")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="blob not found")
    # No manual Content-Length — StreamingResponse defaults to chunked encoding
    # (Pitfall 5: a stale size header can RuntimeError mid-stream).
    return StreamingResponse(
        iterfile_from_open_file(f),
        media_type="application/octet-stream",
    )


@router.delete("/buckets/{bucket}/keys/{key}")
async def delete_blob_route(bucket: str, key: str) -> Response:
    _reject_invalid_request(bucket, key)
    final_path = _final_path(bucket, key)
    if not delete_blob(final_path):
        raise HTTPException(status_code=404, detail="blob not found")
    return Response(status_code=204)