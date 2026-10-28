import os
import tempfile


async def write_blob_atomic(final_path: str, chunks) -> int:
    """Stream chunks into a temp file in the same shard dir, fsync, atomic rename.

    The temp file MUST live in the same directory as final_path (same
    filesystem) — os.replace() across mounts raises EXDEV. On any exception the
    temp file is removed and the exception re-raised. Returns total bytes.

    chmod 0o660 before the rename keeps blobs group-readable/writable,
    consistent with the K8s group-0 ownership model (fsGroup: 0, no fixed
    runAsUser) — mkstemp's default 0600 would strand blobs unreadable by any
    UID other than the one that wrote them.
    """
    shard_dir = os.path.dirname(final_path)
    os.makedirs(shard_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=shard_dir, prefix=".upload-")
    total = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in chunks:
                f.write(chunk)
                total += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o660)
        os.replace(tmp_path, final_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return total


def iterfile_from_open_file(f, chunk_size: int = 65536):
    """Sync generator over an already-open file handle, closed in a finally.

    Used by fetch_blob to eliminate the TOCTOU window between an isfile()
    check and the first read — the caller opens the file itself so a missing
    blob raises FileNotFoundError before any response headers are sent.
    """
    try:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        f.close()


def delete_blob(path: str) -> bool:
    """Immediately unlink the blob (no soft-delete); return whether it existed."""
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False