"""D-10 maintenance cut-over policy: bounded queue clear and scoped cache purge.

The supported deploy order is quiesce -> clear queue -> migrate -> purge cache
-> staged start. This module implements the queue-clear and cache-purge steps
against an injected Redis-like client so it can be exercised offline.

Safety contract:

* Only bounded ``SCAN`` (``scan_iter(..., count=_BATCH)``) is ever used. There
  is no ``KEYS``, no ``FLUSHALL`` and no whole-database delete.
* The queue clear is scoped to the ARQ queue key plus the five ARQ state key
  families only.
* The cache purge is scoped to a fixed pattern allow-list and additionally
  skips any key under a preserved prefix, even when a purge pattern matches it.
* The legacy AI-translation cleanup is scoped to the two retired job function
  names and the exact old status key families only; it never clears the queue or
  purges caches and never touches unrelated jobs or protected keys.
* Every Redis error propagates. In maintenance mode the caller therefore exits
  non-zero and never reports a partial or interrupted run as success.

No application ORM module is imported and no Redis connection is opened at
import time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from arq.constants import (
    default_queue_name as _ARQ_DEFAULT_QUEUE,
    in_progress_key_prefix as _ARQ_IN_PROGRESS_PREFIX,
    job_key_prefix as _ARQ_JOB_PREFIX,
    result_key_prefix as _ARQ_RESULT_PREFIX,
    retry_key_prefix as _ARQ_RETRY_PREFIX,
)
from arq.jobs import deserialize_job
from sqlalchemy import text

# Literal ARQ queue/state key shapes (arq.constants). Mirrored here so the
# policy is importable without a live ARQ pool.
ARQ_QUEUE_KEY = "arq:queue"
ARQ_KEY_PREFIXES: tuple[str, ...] = (
    "arq:job:",
    "arq:in-progress:",
    "arq:result:",
    "arq:retry:",
    "arq:abort",
)

# Cache/tag namespaces purged by the cut-over. ``cache:public:*`` and the bare
# ``public:*`` identity prefix are deliberately listed as separate patterns:
# they are not the same namespace. A bare ``public:*`` key normally does not
# exist (public rate-limit counters live under ``LIMITS:``), so its purge is an
# expected zero-count no-op.
PURGE_PATTERNS: tuple[str, ...] = (
    "cache:*",
    "tag:*",
    "cache:public:*",
    "public:*",
)

# Namespaces that must survive every purge. Auth/session revocation, rate-limit
# counters, the public-token lookup cache and plugin lifecycle state are not
# disposable cut-over artifacts.
PRESERVED_PREFIXES: tuple[str, ...] = (
    "token_blocklist:",
    "LIMITS:",
    "cache:public:token:",
    "plugin_state:",
)

_BATCH = 500

# --- Legacy AI-translation cleanup (307-06, T-307-25) ------------------------
#
# The retired Core AI-translation jobs wrote queue entries (ARQ function names)
# and a fixed set of status keys. This cleanup removes exactly those and nothing
# else: the two legacy function names, and the exact status key families below.
# Unrelated queued jobs and protected keys (token blocklist, rate limits, plugin
# lifecycle state) are never touched, and no broad ``clear_queue_state`` or
# ``purge_caches`` call is ever made from here.
LEGACY_TRANSLATION_JOB_FUNCTIONS: frozenset[str] = frozenset(
    {"translate_schema_job", "translate_ui_strings_job"}
)
LEGACY_TRANSLATION_STATUS_PREFIXES: tuple[str, ...] = (
    "translate_job:",
    "translate_job_id:",
    "translate_job_processed:",
    "translate_job_total:",
    "translate_job_error:",
    "ui_translation_job:",
    "ui_translation_job_scope:",
    "ui_translation_job_lang:",
    "ui_translation_job_total:",
    "ui_translation_job_processed:",
    "ui_translation_job_error:",
)

ORPHANED_TASK_MESSAGE = "Cleared by maintenance cut-over (queue reset)"

# Clearing the ARQ queue orphans every non-template Task that was still waiting
# or running: no job will ever pick it up again and startup recovery only
# handles stale RUNNING rows. Fail them explicitly so they never hang.
_FAIL_ORPHANED_TASKS_SQL = text(
    "UPDATE tasks SET current_status = 'FAILED',"
    " error_message = :message,"
    " completed_at = timezone('utc', now())"
    " WHERE current_status IN ('PENDING', 'RUNNING')"
    " AND is_permanent = false"
)

_STEP_ORDER: tuple[str, ...] = (
    "quiesce: stop worker/scheduler and other queue producers",
    "clear ARQ queue membership and job/in-progress/result/retry/abort state",
    "run the shared Core migration",
    "purge caches: cache:*, tag:*, cache:public:* and public:*",
    "staged start: reload final backend/plugin files and restart workers",
)


class SessionLike(Protocol):
    """Narrow async session surface the orphaned-task step relies on."""

    async def execute(self, statement: Any, params: Any = None) -> Any: ...

    async def commit(self) -> None: ...


class RedisLike(Protocol):
    """Narrow async Redis surface the maintenance steps rely on."""

    async def get(self, key: str) -> object: ...

    async def delete(self, *keys: str) -> int: ...

    def scan_iter(
        self, match: str | None = None, count: int | None = None
    ) -> AsyncIterator[str]: ...


class ArqRedisLike(RedisLike, Protocol):
    """Adds the queue-membership surface the legacy cleanup relies on."""

    async def zrange(self, key: str, start: int, end: int) -> list[Any]: ...

    async def zrem(self, key: str, *members: str) -> int: ...


def plan_steps() -> list[str]:
    """Return the ordered, human-readable cut-over step list."""
    return list(_STEP_ORDER)


def _is_preserved(key: str) -> bool:
    """True when ``key`` is under a preserved prefix and must survive a purge."""
    return key.startswith(PRESERVED_PREFIXES)


async def _delete_batched(redis: RedisLike, keys: list[str]) -> int:
    """Delete ``keys`` in bounded chunks, returning the deleted count."""
    deleted = 0
    for start in range(0, len(keys), _BATCH):
        chunk = keys[start : start + _BATCH]
        if chunk:
            deleted += await redis.delete(*chunk)
    return deleted


async def clear_queue_state(redis: RedisLike) -> int:
    """Delete the ARQ queue key and all ARQ job/state keys.

    Scoped to the queue key plus the five ARQ key families via bounded
    ``SCAN``; returns the number of deleted keys. Any Redis error propagates
    (fail-closed).
    """
    keys: list[str] = [ARQ_QUEUE_KEY]
    for prefix in ARQ_KEY_PREFIXES:
        async for key in redis.scan_iter(match=f"{prefix}*", count=_BATCH):
            keys.append(key)

    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return await _delete_batched(redis, unique)


async def fail_orphaned_tasks(session: SessionLike) -> int:
    """Mark every non-permanent PENDING/RUNNING Task as FAILED after a queue clear.

    Returns the number of updated rows. Any database error propagates
    (fail-closed).
    """
    result = await session.execute(
        _FAIL_ORPHANED_TASKS_SQL, {"message": ORPHANED_TASK_MESSAGE}
    )
    await session.commit()
    return result.rowcount


async def purge_caches(redis: RedisLike) -> int:
    """Purge matching cache/tag/public keys while preserving protected state.

    A key under any ``PRESERVED_PREFIXES`` entry is skipped even when a purge
    pattern matches it. Bounded ``SCAN`` only; returns the deleted count and
    propagates any Redis error (fail-closed).
    """
    deleted = 0
    for pattern in PURGE_PATTERNS:
        keys: list[str] = []
        async for key in redis.scan_iter(match=pattern, count=_BATCH):
            if _is_preserved(key):
                continue
            keys.append(key)
        deleted += await _delete_batched(redis, keys)
    return deleted


async def run_cutover(redis: RedisLike) -> dict:
    """Run the ordered queue-clear then cache-purge steps.

    Returns the per-step counts plus ``ok``. Any Redis error propagates so the
    caller exits non-zero and never reports success for a partial run.
    """
    report: dict = {"ok": False, "steps": {}}
    report["steps"]["clear_queue"] = await clear_queue_state(redis)
    report["steps"]["purge_cache"] = await purge_caches(redis)
    report["ok"] = True
    return report


def _queue_member_job_id(member: object) -> tuple[str, str]:
    """Return ``(job_id, job_key)`` for one ARQ queue member.

    ARQ stores the bare ``job_id`` as the sorted-set member; the job bytes live
    under ``arq:job:<job_id>``. A member already carrying the job-key prefix is
    accepted too, so the helper is robust to either shape.
    """
    text_member = member.decode() if isinstance(member, bytes) else str(member)
    if text_member.startswith(_ARQ_JOB_PREFIX):
        return text_member[len(_ARQ_JOB_PREFIX) :], text_member
    return text_member, f"{_ARQ_JOB_PREFIX}{text_member}"


async def cleanup_legacy_translations(redis: ArqRedisLike) -> dict:
    """Remove ONLY the retired Core AI-translation queue jobs and status keys.

    Two steps, both narrowly scoped (T-307-25):

    1. ``ZRANGE`` the ARQ queue, deserialize each member with ARQ's own
       deserializer and, for the two legacy function names, ``ZREM`` the member
       and delete its ``arq:job:``/``arq:in-progress:``/``arq:retry:``/
       ``arq:result:`` keys. Undecodable entries are skipped and counted rather
       than aborting.
    2. ``SCAN``-delete the exact legacy status families only.

    Returns ``{ok, removed_jobs, removed_keys, skipped_undecodable,
    failed_tasks}``. Legacy jobs never created Task rows, so ``failed_tasks`` is
    always 0 (satisfied by construction). Never calls ``clear_queue_state`` or
    ``purge_caches``; any Redis error propagates (fail-closed).
    """
    removed_jobs = 0
    removed_keys = 0
    skipped_undecodable = 0

    members = await redis.zrange(_ARQ_DEFAULT_QUEUE, 0, -1)
    for member in members:
        job_id, job_key = _queue_member_job_id(member)
        raw = await redis.get(job_key)
        if raw is None:
            skipped_undecodable += 1
            continue
        try:
            job_def = deserialize_job(raw if isinstance(raw, bytes) else str(raw).encode())
        except Exception:  # undecodable payloads are preserved, never aborted on
            skipped_undecodable += 1
            continue
        if job_def.function not in LEGACY_TRANSLATION_JOB_FUNCTIONS:
            continue
        await redis.zrem(_ARQ_DEFAULT_QUEUE, member)
        removed_keys += await redis.delete(
            job_key,
            f"{_ARQ_IN_PROGRESS_PREFIX}{job_id}",
            f"{_ARQ_RETRY_PREFIX}{job_id}",
            f"{_ARQ_RESULT_PREFIX}{job_id}",
        )
        removed_jobs += 1

    for prefix in LEGACY_TRANSLATION_STATUS_PREFIXES:
        keys: list[str] = []
        async for key in redis.scan_iter(match=f"{prefix}*", count=_BATCH):
            keys.append(key)
        removed_keys += await _delete_batched(redis, keys)

    return {
        "ok": True,
        "removed_jobs": removed_jobs,
        "removed_keys": removed_keys,
        "skipped_undecodable": skipped_undecodable,
        "failed_tasks": 0,
    }
