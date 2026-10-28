"""Context variables that carry correlation fields onto every log record.

``RequestLogState`` is deliberately **mutable**: auth dependencies run in child
tasks of ``BaseHTTPMiddleware`` (a context copy), so they must be able to write
``user_id`` back onto the same object the middleware created (D-17). Job
context is an immutable mapping so a bind always produces a fresh snapshot.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from app.logging.fields import JOB_FIELDS


@dataclass(slots=True)
class RequestLogState:
    request_id: str
    user_id: str | None = None
    security_event: str | None = None
    reason: str | None = None
    permission: str | None = None
    subject_user_id: str | None = None


_request_state: ContextVar[RequestLogState | None] = ContextVar(
    "indramesh_request_log_state", default=None
)

_job_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "indramesh_job_log_context", default=MappingProxyType({})
)


def set_request_state(state: RequestLogState) -> Token:
    return _request_state.set(state)


def reset_request_state(token: Token) -> None:
    _request_state.reset(token)


def get_request_state() -> RequestLogState | None:
    return _request_state.get()


def bind_log_context(**fields: Any) -> Token:
    unknown = set(fields) - set(JOB_FIELDS)
    if unknown:
        raise ValueError(f"unknown log context fields: {sorted(unknown)}")
    merged = dict(_job_context.get())
    merged.update(fields)
    return _job_context.set(MappingProxyType(merged))


def reset_log_context(token: Token) -> None:
    _job_context.reset(token)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = bind_log_context(**fields)
    try:
        yield
    finally:
        reset_log_context(token)


def get_log_context() -> dict[str, Any]:
    context = {
        key: value for key, value in _job_context.get().items() if value is not None
    }
    state = _request_state.get()
    if state is not None:
        context["request_id"] = state.request_id
        if state.user_id is not None:
            context["user_id"] = state.user_id
    return context
