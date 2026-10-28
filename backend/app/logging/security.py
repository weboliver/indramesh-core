"""Security event stream helpers (LOG-06, D-05..D-08, D-17).

Only ``app.logging`` modules and the stdlib are imported: the auth dependencies
call the ``mark_*`` helpers to record *what* happened on the mutable
``RequestLogState``, and the outermost middleware emits exactly one ``security``
record per request once the status is known. Security events go to the log
stream only — never to ``audit_log`` (D-07).

The username typed into the login form is never passed here or to any logger
(D-05): an existing user is attributed by ``user_id`` (UUID), an unknown one
only by ``reason=unknown_user``.
"""

from __future__ import annotations

import logging

from app.logging.context import get_request_state

SECURITY_EVENTS = (
    "login_succeeded",
    "login_failed",
    "token_rejected",
    "permission_denied",
    "api_client_auth_failed",
    "rate_limited",
    "token_revoked",
    "password_changed",
    "user_changed",
    "user_access_changed",
    "role_changed",
    "group_changed",
    "api_client_changed",
    "service_account_changed",
    "record_permission_changed",
)

# (HTTP method, full route template) -> mutation event, 2xx only. The route
# templates match the routers included under /api/v1 in app/main.py; the
# table-integrity test resolves every key against the app's OpenAPI schema.
SECURITY_MUTATIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/auth/revoke"): "token_revoked",
    ("POST", "/api/v1/users/me/change-password"): "password_changed",
    ("POST", "/api/v1/users/"): "user_changed",
    ("PATCH", "/api/v1/users/{user_id}"): "user_changed",
    ("DELETE", "/api/v1/users/{user_id}"): "user_changed",
    ("POST", "/api/v1/users/{user_id}/roles"): "user_access_changed",
    ("DELETE", "/api/v1/users/{user_id}/roles/{role_id}"): "user_access_changed",
    ("POST", "/api/v1/users/{user_id}/groups/{group_id}"): "user_access_changed",
    ("DELETE", "/api/v1/users/{user_id}/groups/{group_id}"): "user_access_changed",
    ("POST", "/api/v1/roles/"): "role_changed",
    ("PATCH", "/api/v1/roles/{role_id}"): "role_changed",
    ("DELETE", "/api/v1/roles/{role_id}"): "role_changed",
    ("POST", "/api/v1/roles/{role_id}/permissions"): "role_changed",
    ("DELETE", "/api/v1/roles/{role_id}/permissions/{permission_id}"): "role_changed",
    ("POST", "/api/v1/groups/"): "group_changed",
    ("PATCH", "/api/v1/groups/{group_id}"): "group_changed",
    ("DELETE", "/api/v1/groups/{group_id}"): "group_changed",
    ("POST", "/api/v1/groups/{group_id}/members/{user_id}"): "group_changed",
    ("DELETE", "/api/v1/groups/{group_id}/members/{user_id}"): "group_changed",
    ("POST", "/api/v1/groups/{group_id}/roles/{role_id}"): "group_changed",
    ("DELETE", "/api/v1/groups/{group_id}/roles/{role_id}"): "group_changed",
    ("POST", "/api/v1/api-clients/"): "api_client_changed",
    ("PATCH", "/api/v1/api-clients/{client_id}"): "api_client_changed",
    ("DELETE", "/api/v1/api-clients/{client_id}"): "api_client_changed",
    ("POST", "/api/v1/api-clients/{client_id}/revoke"): "api_client_changed",
    ("POST", "/api/v1/service-accounts/"): "service_account_changed",
    ("PATCH", "/api/v1/service-accounts/{account_id}"): "service_account_changed",
    ("DELETE", "/api/v1/service-accounts/{account_id}"): "service_account_changed",
    ("POST", "/api/v1/record-permissions/"): "record_permission_changed",
    ("DELETE", "/api/v1/record-permissions/{permission_id}"): "record_permission_changed",
}

_SECURITY_LOGGER = logging.getLogger("security")

# D-19: the mandatory level-change line is emitted through this private handler
# seam. It uses ONLY existing frozen fields (event stays None — SECURITY_EVENTS
# and FROZEN_FIELDS are not extended) and the safe alt->new summary lives in
# ``msg``. Context, redaction and the formatter pipeline stay fully active.
_LEVEL_CHANGE_LOGGER = logging.getLogger("security")

# login_failed / permission_denied / rate_limited are WARNING; every other
# event is INFO (D-08). reason=missing drops token/api-client rejections to
# DEBUG so a routine unauthenticated probe stays invisible at INFO.
_EVENT_LEVELS = {
    "login_failed": logging.WARNING,
    "permission_denied": logging.WARNING,
    "rate_limited": logging.WARNING,
}


def mark_authenticated(user_id: str) -> None:
    state = get_request_state()
    if state is not None:
        state.user_id = user_id


def mark_token_rejected(reason: str) -> None:
    state = get_request_state()
    if state is not None:
        state.security_event = "token_rejected"
        state.reason = reason


def mark_api_client_rejected(reason: str) -> None:
    state = get_request_state()
    if state is not None:
        state.security_event = "api_client_auth_failed"
        state.reason = reason


def mark_permission_denied(
    permission: str | None, reason: str = "missing_permission"
) -> None:
    state = get_request_state()
    if state is not None:
        state.security_event = "permission_denied"
        state.reason = reason
        state.permission = permission


def mark_login_succeeded(user_id: str) -> None:
    state = get_request_state()
    if state is not None:
        state.user_id = user_id
        state.security_event = "login_succeeded"


def mark_login_failed(reason: str, user_id: str | None = None) -> None:
    """Record a failed login without ever touching the submitted username (D-05).

    ``user_id`` is the resolved UUID of an *existing* user (the record's
    ``subject_user_id``); an unknown user leaves it unset.
    """
    state = get_request_state()
    if state is not None:
        state.security_event = "login_failed"
        state.reason = reason
        state.subject_user_id = user_id


def emit_security_record(
    state,
    *,
    method: str | None,
    route: str | None,
    status: int | None,
    client_ip: str | None,
) -> None:
    """Emit at most one security record for the finished request."""
    event = state.security_event
    reason = state.reason
    permission = state.permission

    if event is None:
        if status == 429:
            event = "rate_limited"
        elif status == 401:
            event = "token_rejected"
            reason = reason or "invalid"
        elif status == 403:
            event = "permission_denied"
            reason = reason or "forbidden"
        elif status is not None and 200 <= status < 300:
            event = SECURITY_MUTATIONS.get((method, route))

    if event is None:
        return

    level = _EVENT_LEVELS.get(event, logging.INFO)
    if reason == "missing" and event in ("token_rejected", "api_client_auth_failed"):
        level = logging.DEBUG

    _SECURITY_LOGGER.log(
        level,
        "security event",
        extra={
            "event": event,
            "reason": reason,
            "permission": permission,
            "user_id": state.user_id or state.subject_user_id,
            "client_ip": client_ip,
            "method": method,
            "route": route,
            "status": status,
        },
    )


def _format_override(value) -> str:
    if value is None:
        return "cleared"
    if isinstance(value, dict):
        level = value.get("level")
        expires_at = value.get("expires_at")
        return f"{level}@{expires_at}" if expires_at else str(level)
    return str(value)


def emit_level_change_record(
    old_values: dict,
    new_values: dict,
    actor_id=None,
    request_context: dict | None = None,
) -> None:
    """Emit exactly one mandatory security line for a logging-level change.

    D-19: this is the ONE additive exception to the frozen level threshold.
    ``Logger.handle()`` skips the origin logger's level check but still runs
    through every filter and handler, so the record is rendered through the
    normal Context/Redaction/formatter pipeline even when the ``security``
    category is pinned to ERROR/CRITICAL. The alt->new diff lives in ``msg``;
    ``event`` stays ``None`` (no new SECURITY_EVENTS value). This helper is only
    called after a committed, non-no-op change.
    """
    context = request_context or {}
    categories = sorted(set(old_values) | set(new_values))
    summary = "; ".join(
        f"{category} {_format_override(old_values.get(category))}"
        f"->{_format_override(new_values.get(category))}"
        for category in categories
    )
    message = f"logging levels changed: {summary}" if summary else "logging levels changed"

    record = _LEVEL_CHANGE_LOGGER.makeRecord(
        _LEVEL_CHANGE_LOGGER.name,
        logging.INFO,
        __file__,
        0,
        message,
        (),
        None,
    )
    record.event = None
    record.reason = None
    record.permission = None
    record.user_id = str(actor_id) if actor_id is not None else None
    record.client_ip = context.get("client_ip")
    record.method = context.get("method")
    record.route = context.get("route")
    record.status = context.get("status")
    _LEVEL_CHANGE_LOGGER.handle(record)
