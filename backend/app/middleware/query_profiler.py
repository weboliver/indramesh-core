"""SQLAlchemy query profiler with per-request query tracking.

Tracks per-request SQL query counts. Uses a module-level counter shared
across asyncio tasks because Starlette's BaseHTTPMiddleware (v1.3.1) creates
separate tasks via anyio.create_task_group(), which prevents contextvars
from propagating from the outer middleware task to the inner route-handler
task where SQLAlchemy event listeners fire.

Race condition warning: under concurrent load the counter may be briefly
reset by a concur rent request's reset_query_count(). For development
tooling this is acceptable — count inaccuracies are cosmetic only.
"""

import logging
import time
import traceback

from app.config import settings
from contextvars import ContextVar

from sqlalchemy import event

from app.db import engine

logger = logging.getLogger(__name__)

# NOTE: Module-level counter (not ContextVar) because SQLAlchemy's
# greenlet_spawn runs in a different asyncio task than the outer middleware
# when Starlette's BaseHTTPMiddleware is in the middleware chain.
# ContextVars are per-asyncio-Task and don't propagate across this boundary.
_global_query_count: int = 0

_slow_query_threshold_ms: ContextVar[float] = ContextVar(
    "slow_query_threshold_ms", default=100.0
)


def reset_query_count(threshold_ms: float = 100.0) -> None:
    """Reset the query counter for a new request and set threshold."""
    global _global_query_count
    _global_query_count = 0
    _slow_query_threshold_ms.set(threshold_ms)


def increment_query_count() -> None:
    """Increment the query counter (called by SQLAlchemy event listener)."""
    global _global_query_count
    _global_query_count += 1


def get_query_count() -> int:
    """Return the current query count for this request."""
    return _global_query_count


def slow_query(duration_ms: float, sql: str) -> None:
    """Log a slow SQL query with duration, SQL text, and originating stack.

    T-102-02: SQL text is only logged when ENVIRONMENT=development — never
    in production, regardless of the root logger's configured level.
    """
    if settings.environment != "development":
        return
    threshold = _slow_query_threshold_ms.get()
    if duration_ms > threshold:
        stack = "".join(traceback.format_stack())
        logger.debug(
            "Slow SQL (%.1fms): %s\nStack:\n%s",
            duration_ms,
            sql,
            stack,
        )


# Track start times per connection id for duration measurement
_query_start_times: dict[int, float] = {}


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    """Increment query counter and record start time before execution."""
    increment_query_count()
    _query_start_times[id(conn)] = time.perf_counter()


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    """Measure duration and check for slow query after execution."""
    start = _query_start_times.pop(id(conn), None)
    if start is not None:
        duration_ms = (time.perf_counter() - start) * 1000
        slow_query(duration_ms, statement)
