"""Fail-closed maintenance CLI: ``python -m app.maintenance <command>``.

Commands (bounded; there is no ``--all``/``--flush``/namespace override):

* ``clear-queue``  — clear ARQ queue membership and job/state keys only, then
  mark the Tasks orphaned by that clear (non-permanent PENDING/RUNNING) FAILED.
* ``purge-cache``  — purge the fixed cache/tag/public namespaces only.
* ``cleanup-legacy-translations`` — remove ONLY the retired Core AI-translation
  queue jobs (by deserialized ARQ function name) and the exact old status key
  families; unrelated jobs and protected keys survive.
* ``cutover``      — the ordered queue-clear then cache-purge sequence.
* ``revalidate-saved-queries`` — read-only, DB-only: list every stored saved
  query the Phase-312 GraphQL limits would now reject (``--json`` for the JSON
  document). Exit codes: ``0`` clean, ``1`` at least one breach, ``2`` the run
  itself failed. Redis is deliberately not initialised for this verb.
* ``rotate-fernet-key`` — re-encrypt every stored versioned ciphertext the
  previous key still protects under ``FERNET_KEY``, then prove in a fresh
  session that ``FERNET_KEY`` alone reads every stored value. ``--check``
  verifies only and writes nothing. Exit codes: ``0`` safe (the old key can be
  removed), ``1`` not safe, ``2`` the run itself failed. Redis is deliberately
  not initialised for this verb.

Any Redis error propagates to a non-zero exit code and no success record is
logged, so a partial or interrupted run can never look like a successful
cut-over. Completion and failure are structured ``app`` records (D-10), not
``print`` output — the operator table of ``revalidate-saved-queries`` is the
one deliberate exception, because its whole output IS the report.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.db.redis import close_redis, get_redis, init_redis
from app.logging import configure_logging
from app.maintenance import cutover

logger = logging.getLogger(__name__)

REVALIDATE_COMMAND = "revalidate-saved-queries"
"""The one DB-only, Redis-free verb (D-15/D-16)."""

REVALIDATE_FAILURE_EXIT_CODE = 2
"""``revalidate-saved-queries``' own failure code: distinct from a violation (1).

The historical cut-over verbs keep their fail-closed ``0``/``1`` contract
(``test_logging_canary`` pins it); only this read-only operator tool separates
"violations found" from "the tool itself failed".
"""

ROTATE_COMMAND = "rotate-fernet-key"
"""The Fernet key-rotation verb (SEC313-69)."""

ROTATE_FAILURE_EXIT_CODE = 2
"""``rotate-fernet-key``' failure code: ``1`` means "not safe", not "broken"."""

COMMANDS = (
    "clear-queue",
    "purge-cache",
    "cleanup-legacy-translations",
    "cutover",
    REVALIDATE_COMMAND,
    ROTATE_COMMAND,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.maintenance",
        description="Bounded D-10 maintenance cut-over steps (fail-closed).",
    )
    parser.add_argument(
        "command",
        choices=COMMANDS,
        help="bounded maintenance step to run",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the report as JSON (read by revalidate-saved-queries only)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify only, no writes (read by rotate-fernet-key only)",
    )
    return parser


async def _fail_orphaned_tasks() -> int:
    from app.db import async_session_maker, engine

    try:
        async with async_session_maker() as session:
            return await cutover.fail_orphaned_tasks(session)
    finally:
        await engine.dispose()


async def _revalidate_saved_queries(*, as_json: bool) -> dict:
    """Run the read-only revalidation verb without touching Redis (D-15/D-16).

    A DB-only operator tool must run in a DB-only context, so this path skips
    the ``init_redis()``/``close_redis()`` pair of ``_run``. The session opens,
    reads and prints; nothing is written and no cache is invalidated.
    """
    from app.db import async_session_maker, engine
    from app.maintenance import revalidate

    try:
        async with async_session_maker() as session:
            reports = await revalidate.revalidate_saved_queries(session)
    finally:
        await engine.dispose()

    logger.info(
        "saved-query revalidation report",
        extra={
            "command": "revalidate-saved-queries",
            "report_text": (
                revalidate.format_report_json(reports)
                if as_json
                else revalidate.format_report(reports)
            ),
        },
    )
    return revalidate.build_report(reports)


async def _rotate_fernet_key(*, check: bool) -> dict:
    """Run the Fernet rotation verb without touching Redis (SEC313-69).

    DB-only like ``revalidate-saved-queries``: the pass reads and writes raw
    ciphertext, prints its operator report and returns the report carrying the
    exit code. No ``init_redis()``/``close_redis()`` pair — a maintenance run
    must not depend on the cache.
    """
    from app.db import async_session_maker, engine
    from app.maintenance import fernet_rotation

    try:
        report = await fernet_rotation.run_rotation(async_session_maker, check=check)
    finally:
        await engine.dispose()
    logger.info(
        "fernet rotation report",
        extra={
            "command": "rotate-fernet-key",
            "report_text": fernet_rotation.format_report(report),
        },
    )
    return report


async def _run(command: str, *, as_json: bool = False, check: bool = False) -> dict:
    if command == ROTATE_COMMAND:
        return await _rotate_fernet_key(check=check)
    if command == REVALIDATE_COMMAND:
        return await _revalidate_saved_queries(as_json=as_json)
    await init_redis()
    try:
        redis = await get_redis()
        if command == "clear-queue":
            cleared = await cutover.clear_queue_state(redis)
            failed = await _fail_orphaned_tasks()
            return {"ok": True, "clear_queue": cleared, "failed_tasks": failed}
        if command == "purge-cache":
            return {"ok": True, "purge_cache": await cutover.purge_caches(redis)}
        if command == "cleanup-legacy-translations":
            return await cutover.cleanup_legacy_translations(redis)
        report = await cutover.run_cutover(redis)
        report["steps"]["failed_tasks"] = await _fail_orphaned_tasks()
        return report
    finally:
        await close_redis()


def main(argv: list[str] | None = None) -> int:
    configure_logging("maintenance")
    args = build_parser().parse_args(argv)
    if args.command == ROTATE_COMMAND:
        failure_code = ROTATE_FAILURE_EXIT_CODE
    elif args.command == REVALIDATE_COMMAND:
        failure_code = REVALIDATE_FAILURE_EXIT_CODE
    else:
        failure_code = 1
    try:
        if args.command == ROTATE_COMMAND:
            report = asyncio.run(_run(args.command, check=args.check))
        elif args.command == REVALIDATE_COMMAND:
            report = asyncio.run(_run(args.command, as_json=args.json))
        else:
            report = asyncio.run(_run(args.command))
    except Exception:  # maintenance mode fails closed
        logger.error(
            "maintenance command failed",
            extra={"command": args.command},
            exc_info=True,
        )
        return failure_code
    # A reported violation is a completed READ with a non-zero exit code — the
    # command itself worked, so it is logged as an error record carrying the
    # report (the report has no query text; see app.maintenance.revalidate).
    exit_code = report.get("exit_code") or 0
    if report.get("ok") and not exit_code:
        logger.info(
            "maintenance command finished",
            extra={"command": args.command, "report": report},
        )
        return 0
    logger.error(
        "maintenance command finished",
        extra={"command": args.command, "report": report},
    )
    return exit_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
