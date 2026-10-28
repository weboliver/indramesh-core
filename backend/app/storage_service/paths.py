import os
import uuid


def validate_key(key: str) -> None:
    """Raise ValueError unless key parses as a UUID.

    D-13 keys are always server-generated UUIDs, so a UUID-format check is a
    cheap and sufficient primary defense against path traversal (T-278-01-01).
    """
    uuid.UUID(key)


def validate_bucket(bucket: str, allowed_bucket: str) -> None:
    """Raise ValueError unless bucket matches the single configured bucket (D-08).

    A mismatch answers 404, never silent directory creation.
    """
    if bucket != allowed_bucket:
        raise ValueError(f"unknown bucket: {bucket}")


def resolve_shard_path(blob_root: str, bucket: str, key: str) -> str:
    """D-14 layout: {blob_root}/{bucket}/{key[:2]}/{key} (UUID-prefix shards)."""
    return os.path.join(blob_root, bucket, key[:2], key)


def assert_contained(path: str, blob_root: str) -> None:
    """Defense-in-depth realpath containment check (mirrors exports.py CR-01).

    Raises ValueError if the resolved path escapes the blob root.
    """
    resolved = os.path.realpath(path)
    if resolved != blob_root and not resolved.startswith(blob_root + os.sep):
        raise ValueError("path escapes blob root")