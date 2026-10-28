from typing import AsyncIterator, Protocol

import httpx

from app.config import storage_settings


class StorageClient(Protocol):
    async def store(self, bucket: str, key: str, chunks) -> int: ...
    async def fetch(self, bucket: str, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, bucket: str, key: str) -> None: ...


class RestStorageClient:
    """HTTP implementation of the StorageClient seam (D-07, D-05).

    Mirrors JiraClient's long-lived httpx.AsyncClient: this client is called
    far more often than a per-call factory would justify (D-10).
    """

    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(30.0)
        )

    async def store(self, bucket: str, key: str, chunks) -> int:
        # httpx streams an async byte generator request body without buffering it (D-09).
        response = await self._client.put(
            f"/buckets/{bucket}/keys/{key}", content=chunks
        )
        response.raise_for_status()
        return int(response.headers.get("X-Stored-Bytes", "0"))

    async def fetch(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        async with self._client.stream(
            "GET", f"/buckets/{bucket}/keys/{key}"
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    async def delete(self, bucket: str, key: str) -> None:
        response = await self._client.delete(f"/buckets/{bucket}/keys/{key}")
        # 404 = already gone (storage service unlinks immediately; idempotent caller).
        if response.status_code not in (204, 404):
            response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


_client: StorageClient | None = None


def get_storage_client() -> StorageClient:
    """Lazy module-level singleton — reads storage_settings.storage_service_url on first call (never at import time)."""
    global _client
    if _client is None:
        _client = RestStorageClient(storage_settings.storage_service_url)
    return _client