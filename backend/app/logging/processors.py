"""structlog ProcessorFormatter builders for json/logfmt/console (D-15)."""

import json

import structlog

from app.logging.redaction import redact_event_dict

# Docker's json-file driver splits lines above 16 KB, so an oversized message or
# rendered exception would break the collector's one-record-per-line contract
# (research Pitfall 13, orchestrator resolution 5).
MAX_MESSAGE_CHARS = 8192
MAX_EXCEPTION_CHARS = 8192
MAX_TRACEBACK_FRAMES = 20

# ProcessorFormatter copies the record before formatting and ExtraAdder copies
# every non-standard attribute, so a record already formatted by another handler
# (pytest's caplog, later the task-log handler of phase 300) would otherwise
# carry a duplicate, unredacted ``message`` key. uvicorn passes ANSI text as
# ``color_message``.
_INTERNAL_KEYS = ("message", "asctime", "color_message")


def drop_internal_keys(logger, method_name, event_dict):
    for key in _INTERNAL_KEYS:
        event_dict.pop(key, None)
    return event_dict


def _truncate(value: str) -> str:
    if len(value) <= MAX_MESSAGE_CHARS:
        return value
    dropped = len(value) - MAX_MESSAGE_CHARS
    return value[:MAX_MESSAGE_CHARS] + f" …[truncated {dropped} chars]"


def cap_message(logger, method_name, event_dict):
    """Cap ``msg`` and every non-exempt value to the per-value char budget.

    ``extra`` payloads are unbounded and can individually push a record over the
    16 KB json-file split — the import execution service logs up to 25 raw error
    strings in one ``errors`` list. A non-string value is measured by its
    serialized size and replaced by its truncated JSON text when oversized, so
    the one-record-per-line contract holds.
    """
    for key, value in event_dict.items():
        if key in _INTERNAL_KEYS or key == "exception":
            continue
        if isinstance(value, str):
            event_dict[key] = _truncate(value)
            continue
        try:
            serialized = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        if len(serialized) > MAX_MESSAGE_CHARS:
            event_dict[key] = _truncate(serialized)
    return event_dict


def cap_exception(logger, method_name, event_dict):
    """Keep the tail of a rendered exception (newest frames plus the error line)."""
    exc = event_dict.get("exception")
    if isinstance(exc, str) and len(exc) > MAX_EXCEPTION_CHARS:
        event_dict["exception"] = "…[truncated] " + exc[-MAX_EXCEPTION_CHARS:]
    return event_dict


def escape_control_chars(logger, method_name, event_dict):
    """Escape raw CR/LF in logfmt/console values so one record stays one line.

    The rendered ``exception`` block is left untouched (it is deliberately
    multi-line); JSON escapes CR/LF natively and never uses this processor.
    """
    for key, value in event_dict.items():
        if key == "exception" or not isinstance(value, str):
            continue
        event_dict[key] = value.replace("\r", "\\r").replace("\n", "\\n")
    return event_dict


def _foreign_pre_chain():
    return [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        # EventRenamer runs BEFORE ExtraAdder so an ``extra={"event": ...}``
        # value can never overwrite the message (the frozen ``event`` field).
        structlog.processors.EventRenamer("msg"),
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    ]


def build_formatter(fmt: str) -> structlog.stdlib.ProcessorFormatter:
    common = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        drop_internal_keys,
    ]

    if fmt == "json":
        processors = common + [
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(
                    show_locals=False, max_frames=MAX_TRACEBACK_FRAMES
                )
            ),
            redact_event_dict,
            cap_message,
            structlog.processors.JSONRenderer(),
        ]
    elif fmt == "logfmt":
        processors = common + [
            structlog.processors.format_exc_info,
            redact_event_dict,
            cap_message,
            cap_exception,
            escape_control_chars,
            structlog.processors.LogfmtRenderer(
                key_order=["ts", "level", "category", "logger", "msg"],
                drop_missing=True,
            ),
        ]
    elif fmt == "console":
        # structlog's default console layout renders ts/level/logger/msg as
        # un-labelled columns, so it cannot carry the same frozen key set as
        # json/logfmt (plan 298-03 format-parity truth). A single key=value
        # column keeps every field labelled and the line human-readable; the
        # exception block is still appended by ConsoleRenderer itself.
        styles = structlog.dev.ConsoleRenderer.get_default_column_styles(
            colors=False, force_colors=False
        )
        key_value = structlog.dev.KeyValueColumnFormatter(
            styles.kv_key,
            styles.kv_value,
            styles.reset,
            value_repr=str,
            width=0,
        )
        processors = common + [
            structlog.processors.format_exc_info,
            redact_event_dict,
            cap_message,
            cap_exception,
            escape_control_chars,
            structlog.dev.ConsoleRenderer(
                columns=[structlog.dev.Column("", key_value)],
            ),
        ]
    else:
        raise ValueError(f"unsupported log format: {fmt!r}")

    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_foreign_pre_chain(),
        processors=processors,
    )
