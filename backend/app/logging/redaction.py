"""Structural secret redaction applied before every renderer (D-09, LOG-07).

This module replaces the former stdout-wrapper module (D-09): the same
field=value/Bearer regexes now run as pure per-record transformations on the
structured event dict and on a record's mapping/tuple args, so no secret can be
observed, bypassed or used to break a JSON line. Stdlib only.
"""

import logging
import re
from typing import Any, Mapping

REDACTED = "[REDACTED]"

# Field=value redaction pattern: catches field names followed by : or = and a
# contiguous non-whitespace value. Applied after the standalone Bearer pattern so
# that "Authorization: Bearer xyz789" headers are handled correctly (the Bearer
# pattern catches "Bearer xyz789" first, then the field pattern consumes the
# remaining "authorization: Bearer=[REDACTED]" as a unit). The optional quotes
# around the key and the value let it also match a Python/JSON mapping repr
# ("'password': 'secret'" / '"password": "secret"'), where a quote sits between
# the key and the separator.
_SENSITIVE_PATTERNS = re.compile(
    r"""['"]?(source_bearer_token|target_bearer_token|password|token|bearer|authorization|"""
    r"""api_key|JWT_SECRET_KEY|FERNET_KEY)['"]?\s*[:=]\s*['"]?\S+""",
    re.IGNORECASE,
)

# Standalone Bearer-header pattern: "Authorization: Bearer <token>". Must run
# BEFORE the field=value pattern, whose \S+ would otherwise stop at the space
# and leave the token exposed.
_BEARER_PATTERN = re.compile(r'Bearer\s+\S+', re.IGNORECASE)

_REDACTED_BEARER = 'Bearer=[REDACTED]'

# URL userinfo: scheme://user:password@host -> scheme://user:[REDACTED]@host.
_URL_USERINFO_PATTERN = re.compile(
    r'(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^:/@\s]*):(?P<password>[^@/\s]+)@'
)

# Compared after lowercasing and replacing "-" with "_". Deliberately no
# substring rule on "token" and no "_key" suffix, so prompt_tokens,
# completion_tokens, object_key and cache_key survive.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "password_hash",
        "secret",
        "token",
        "token_hash",
        "bearer",
        "authorization",
        "cookie",
        "set_cookie",
        "credentials",
        "api_key",
        "apikey",
        "client_secret",
        "secret_key",
        "private_key",
        "jwt_secret_key",
        "fernet_key",
        "dsn",
        "database_url",
        "connection_string",
        "source_bearer_token",
        "target_bearer_token",
    }
)

# Credential-shaped suffixes. ``_confirmation`` and ``_private_key`` cover
# password-confirmation fields and private-key material that the exact-name set
# would otherwise miss.
SENSITIVE_KEY_SUFFIXES = (
    "_password",
    "_secret",
    "_token",
    "_api_key",
    "_confirmation",
    "_private_key",
)

# camelCase/PascalCase -> snake_case boundary: an underscore is inserted only
# between a lowercase/digit and a following uppercase, so an already-UPPER_SNAKE
# key (JWT_SECRET_KEY) is not mangled into J_W_T_... and keeps matching.
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_MAX_REDACT_DEPTH = 6

_EXEMPT_KEYS = ("ts", "level", "logger", "category")


def _redact(match: re.Match) -> str:
    """Redaction callback for the field=value pattern."""
    return f"{match.group(1)}=[REDACTED]"


def _redact_userinfo(match: re.Match) -> str:
    return f"{match.group('scheme')}{match.group('user')}:{REDACTED}@"


def is_sensitive_key(key: Any) -> bool:
    # A non-string key can never name a credential, and redaction must never
    # raise: the stdlib handler's handleError would discard the whole record.
    if not isinstance(key, str):
        return False
    normalized = (
        _CAMEL_CASE_BOUNDARY.sub("_", key).lower().replace("-", "_")
    )
    if normalized in SENSITIVE_KEYS:
        return True
    return normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_text(text: str) -> str:
    """Apply the field=value and URL-userinfo redactions to a message string."""
    text = _BEARER_PATTERN.sub(_REDACTED_BEARER, text)
    text = _SENSITIVE_PATTERNS.sub(_redact, text)
    text = _URL_USERINFO_PATTERN.sub(_redact_userinfo, text)
    return text


def redact_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact strings and sensitive keys up to ``_MAX_REDACT_DEPTH``."""
    if depth > _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive_key(key) else redact_value(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [redact_value(item, depth + 1) for item in value]
        return tuple(redacted) if isinstance(value, tuple) else redacted
    return value


class RedactionFilter(logging.Filter):
    """Redact a record's args before its message is formatted.

    This is the only point where key-based redaction of mapping args is
    possible: ``logger.info("%s", {"password": ...})`` unwraps the mapping into
    ``record.args``. Args are redacted through ``redact_value`` so a nested
    mapping/list under a non-sensitive key is redacted too, not just the top
    level. The filter never drops the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, Mapping):
            record.args = {
                key: (
                    REDACTED if is_sensitive_key(key) else redact_value(value)
                )
                for key, value in args.items()
            }
        elif isinstance(args, tuple):
            record.args = tuple(redact_value(value) for value in args)
        return True


def redact_event_dict(logger, method_name, event_dict):
    """structlog processor: redact every non-exempt key of the event dict."""
    for key in list(event_dict):
        if key in _EXEMPT_KEYS:
            continue
        if is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact_value(event_dict[key])
    return event_dict
