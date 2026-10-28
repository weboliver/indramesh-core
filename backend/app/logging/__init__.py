"""Central, idempotent logging configuration for every IndraMesh process.

The package reads ``os.environ`` directly and never imports application
settings, so the storage service and CLI processes can use it too. It keeps the
stdlib ``logging`` module as the backbone and uses structlog only for
rendering, which keeps ``caplog`` and every foreign root handler working.
"""

import logging
import logging.config
import os
import sys

from app.logging.fields import LOG_FORMATS, NOISY_LOGGERS, PROCESS_NAMES
from app.logging.filters import LogContextFilter
from app.logging.processors import build_formatter
from app.logging.redaction import RedactionFilter
from app.logging.levels import install_level_policy

# Marks the handler this module owns so a later configure_logging call can swap
# exactly its own handler and never a foreign one (D-18, research Pattern 1).
_HANDLER_MARKER = "_indramesh_root_handler"

# uvicorn (its LOGGING_CONFIG) and arq (its default "arq.standard") attach their
# own handlers, which would emit duplicate plain-text lines next to ours. Their
# handler lists are cleared programmatically: an incremental dictConfig cannot
# remove handlers, only set level/propagate (research Pitfalls 3 and 4).
FOREIGN_HANDLER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "arq")

# Noisy libraries default to WARNING (D-19); apscheduler would otherwise log
# every interval run at INFO. "app"/"app.*" are deliberately NOT listed so they
# keep no explicit level and caplog.at_level still reaches them. The set lives
# in fields.py so app.logging.levels shares the exact same definition.
_NOISY_LOGGERS = NOISY_LOGGERS

# dictConfig "loggers" section. uvicorn/uvicorn.error/arq propagate so their
# records render once through our root handler; uvicorn.access is muted here
# because the access record comes from RequestContextMiddleware (--no-access-log
# follows in plan 298-07).
LOGGER_SETTINGS: dict[str, dict] = {
    "uvicorn": {"propagate": True},
    "uvicorn.error": {"propagate": True},
    "uvicorn.access": {"propagate": False},
    "arq": {"level": "INFO", "propagate": True},
}
LOGGER_SETTINGS.update(
    {name: {"level": "WARNING", "propagate": True} for name in _NOISY_LOGGERS}
)

_PROCESS_NAME: str | None = None
_REJECTED_FORMAT: str | None = None


def _environment_default() -> str:
    return "json" if os.environ.get("ENVIRONMENT") == "production" else "console"


def resolve_log_format(fmt: str | None = None) -> str:
    """Resolve the active format: explicit arg > LOG_FORMAT env > env default.

    An unrecognised value is rejected (recorded for a warning once the handler
    exists) and falls back to the environment default (D-15).
    """
    global _REJECTED_FORMAT
    _REJECTED_FORMAT = None

    if fmt is not None:
        if fmt in LOG_FORMATS:
            return fmt
        _REJECTED_FORMAT = fmt
        return _environment_default()

    env_fmt = os.environ.get("LOG_FORMAT")
    if env_fmt:
        if env_fmt in LOG_FORMATS:
            return env_fmt
        _REJECTED_FORMAT = env_fmt
        return _environment_default()

    return _environment_default()


def build_handler(fmt: str, stream) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.addFilter(LogContextFilter())
    handler.addFilter(RedactionFilter())
    handler.setFormatter(build_formatter(fmt))
    return handler


def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logging.getLogger("app").critical(
        "uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
    )


def configure_logging(process: str, *, fmt: str | None = None, stream=None) -> str:
    """Install exactly one marked handler on the logger tree and return the format.

    Idempotent: re-running removes only the handler this function previously
    installed. Foreign root handlers (pytest's caplog, a later task-log handler)
    are never touched.
    """
    global _PROCESS_NAME
    if process not in PROCESS_NAMES:
        raise ValueError(f"unknown process {process!r}; expected one of {PROCESS_NAMES}")

    resolved = resolve_log_format(fmt)

    for name in FOREIGN_HANDLER_LOGGERS:
        logging.getLogger(name).handlers.clear()
    logging.config.dictConfig(
        {"version": 1, "incremental": True, "loggers": LOGGER_SETTINGS}
    )

    handler = build_handler(
        resolved, stream if stream is not None else sys.__stdout__
    )
    setattr(handler, _HANDLER_MARKER, True)

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    _PROCESS_NAME = process
    # Re-arm warning capture on every call: stdlib captureWarnings(True) is a
    # no-op once installed, so a foreign context (a test harness, a library)
    # that replaced ``warnings.showwarning`` would silently disable it.
    logging.captureWarnings(False)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught

    # 299: install/refresh the runtime level policy AFTER the Foundation
    # dictConfig so the policy's baseline capture observes the noisy WARNING
    # defaults. Idempotent — repeated configure calls re-apply the active policy.
    policy = install_level_policy()

    if _REJECTED_FORMAT is not None:
        logging.getLogger("app.logging").warning(
            "invalid log format %r rejected; using %r", _REJECTED_FORMAT, resolved
        )

    # D-04: report invalid LOG_LEVEL/LOG_LEVELS entries once per configure call,
    # naming only the count and the error codes — never a raw env value, which
    # could carry a secret from a typo.
    rejections = policy.env.rejections
    if rejections:
        logging.getLogger("app.logging").warning(
            "ignored %d invalid log-level environment entr%s: %s",
            len(rejections),
            "y" if len(rejections) == 1 else "ies",
            ",".join(sorted(rejections)),
        )

    return resolved
