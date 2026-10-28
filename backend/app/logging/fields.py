"""Frozen logging field, category, format, trigger and process constants.

These names are frozen for phases 299-307 (research Open Question 2 resolved:
the set includes ``event`` and ``permission``). Stdlib only — importable by
every process without pulling in application settings.
"""

BASE_FIELDS = ("ts", "level", "logger", "category", "msg", "exception")

CONTEXT_FIELDS = (
    "request_id",
    "user_id",
    "task_id",
    "job_id",
    "job_name",
    "attempt",
    "trigger",
    "schedule_id",
    "plugin",
    "task_type",
    "schema_id",
)

# Worker-job correlation fields: the request-only ids are excluded.
JOB_FIELDS = tuple(field for field in CONTEXT_FIELDS if field not in ("request_id", "user_id"))

RECORD_FIELDS = (
    "method",
    "route",
    "status",
    "duration_ms",
    "client_ip",
    "reason",
    "event",
    "permission",
)

FROZEN_FIELDS = BASE_FIELDS + CONTEXT_FIELDS + RECORD_FIELDS

CATEGORIES = ("app", "access", "security", "task", "db", "llm")

CATEGORY_PREFIXES = ("connector.", "plugin.")

LOG_FORMATS = ("json", "logfmt", "console")

TRIGGERS = ("manual", "schedule", "retry")

PROCESS_NAMES = (
    "backend",
    "worker",
    "migration-child",
    "plugin-runner",
    "bootstrap-role",
    "alembic",
    "maintenance",
    "storage",
)

# Noisy third-party loggers the Foundation pins to WARNING (D-19, 298). Named
# here (stdlib-only) so both ``app.logging`` and ``app.logging.levels`` share
# ONE definition: the runtime level policy derives each category's resting
# (member) threshold from exactly this set.
NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "openai",
    "instructor",
    "sqlalchemy",
    "opensearch",
    "opensearchpy.helpers",
    "urllib3",
    "watchfiles",
    "apscheduler",
)
