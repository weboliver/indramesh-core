"""Runtime log-level policy: env basis, DB overrides, TTL and the controller.

This module is deliberately dependency-light: besides ``app.logging.categories``
and the stdlib it imports nothing application-specific at module scope, so the
storage/alembic processes can import the env parser without pulling in settings
or database code. The database and Valkey adapters are imported lazily inside
the controller methods (D-14/D-16, research Pattern 8).

Precedence (D-05): DB override > ``LOG_LEVELS[category]`` > global ``LOG_LEVEL``.
Without any explicit configuration the Foundation noisy defaults and normal
logger inheritance are preserved (D-17).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from app.logging.categories import CATEGORY_RE, category_for, is_valid_category
from app.logging.fields import NOISY_LOGGERS

logger = logging.getLogger(__name__)

# The one reserved ``system_settings`` key that carries every level override,
# and the Valkey key every process polls for a change signal (Carry-D7).
LOGGING_LEVELS_KEY = "logging.levels"
LEVEL_VERSION_KEY = "logging:levels:version"

# Plan-02-internal discovery leases live under this prefix; reserved against
# generic writes and never offered as a user-visible category.
DISCOVERY_KEY_PREFIX = "logging.discovery:"

# D-02 vocabulary. WARN is accepted as an alias of WARNING.
LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LEVEL_VALUES: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_LEVEL_ALIASES = {"WARN": "WARNING"}

# D-01: the six fixed core categories, in their frozen display order.
CORE_CATEGORIES = ("app", "access", "security", "task", "db", "llm")
CORE_CATEGORY_ORDER = ("app", "access", "security", "task", "db", "llm")

# D-06/D-23: client-chosen relative duration.
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86400
MAX_CATEGORIES_PER_REQUEST = 128
MAX_BODY_BYTES = 65536

# D-22: the noisy-category threshold is derived from these categories. A noisy
# logger that maps to the generic ``app`` bucket (httpx, urllib3, ...) does NOT
# raise the ``app`` threshold — D-22's documented example is ``app`` = INFO.
_NOISY_CATEGORIES = frozenset(
    category for category in (category_for(name) for name in NOISY_LOGGERS)
    if category != "app"
)

# D-19: recognized env rejection codes (never the raw value).
REJECTION_INVALID_GLOBAL_LEVEL = "invalid_global_level"
REJECTION_MALFORMED_ENTRY = "malformed_entry"
REJECTION_INVALID_CATEGORY = "invalid_category"
REJECTION_INVALID_LEVEL = "invalid_level"


class InvalidLevelError(ValueError):
    """A level string outside the frozen D-02 vocabulary."""


class InvalidLevelSnapshot(ValueError):
    """The persisted ``logging.levels`` value is not a valid override snapshot."""


def normalize_level(value: Any) -> str:
    """Normalize a level string to its canonical Python name (D-02).

    Case-insensitive; ``WARN`` maps to ``WARNING``. ``None``/non-strings and
    unknown names raise ``InvalidLevelError`` so callers can turn that into 422.
    """
    if not isinstance(value, str):
        raise InvalidLevelError("level must be a string")
    normalized = _LEVEL_ALIASES.get(value.strip().upper(), value.strip().upper())
    if normalized not in LEVEL_VALUES:
        raise InvalidLevelError(
            f"level {value!r} is not one of {', '.join(LEVEL_NAMES)}"
        )
    return normalized


def normalize_category(value: Any) -> str:
    """Return the exact category string or raise for a syntactically invalid one."""
    if not isinstance(value, str) or not CATEGORY_RE.fullmatch(value):
        raise InvalidLevelError(f"unknown log category {value!r}")
    return value


@dataclass
class EnvLevelPolicy:
    """The parsed ``LOG_LEVEL``/``LOG_LEVELS`` basis of one process (D-05)."""

    global_level: str | None = None
    category_levels: dict[str, str] = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)

    @property
    def global_explicit(self) -> bool:
        return self.global_level is not None


def parse_env_levels(environ: Mapping[str, str] | None = None) -> EnvLevelPolicy:
    """Parse ``LOG_LEVEL`` and ``LOG_LEVELS`` from ``os.environ`` (or a mapping).

    Never imports ``app.config`` (the logging package's stdlib-only contract).
    Missing/empty ``LOG_LEVEL`` is not explicit; an invalid global value falls
    back to the Foundation. Invalid ``LOG_LEVELS`` entries are collected as
    rejection codes and ignored — the process still starts (D-04).
    """
    env = os.environ if environ is None else environ
    rejections: list[str] = []

    raw_global = env.get("LOG_LEVEL")
    global_level: str | None = None
    if raw_global is not None and raw_global.strip():
        try:
            global_level = normalize_level(raw_global)
        except InvalidLevelError:
            rejections.append(REJECTION_INVALID_GLOBAL_LEVEL)

    category_levels: dict[str, str] = {}
    raw_map = env.get("LOG_LEVELS")
    if raw_map:
        for entry in raw_map.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                rejections.append(REJECTION_MALFORMED_ENTRY)
                continue
            category, _, level = entry.partition("=")
            category = category.strip()
            if not CATEGORY_RE.fullmatch(category):
                rejections.append(REJECTION_INVALID_CATEGORY)
                continue
            try:
                category_levels[category] = normalize_level(level)
            except InvalidLevelError:
                rejections.append(REJECTION_INVALID_LEVEL)

    return EnvLevelPolicy(
        global_level=global_level,
        category_levels=category_levels,
        rejections=rejections,
    )


def build_foundation_levels() -> dict[str, int]:
    """Per-category resting (member) levels of the Foundation noisy defaults.

    Only categories with a *dedicated* foundation member (db/llm/task) get a
    threshold above the env basis; the generic ``app`` bucket stays INFO.
    """
    levels: dict[str, int] = {}
    for name in NOISY_LOGGERS:
        category = category_for(name)
        if category == "app":
            continue
        levels[category] = max(levels.get(category, logging.NOTSET), logging.WARNING)
    return levels


def _foundation_level_for_logger(name: str) -> int:
    """The explicit level a brand-new logger should rest at with no policy.

    A descendant of a Foundation noisy logger inherits WARNING (the seam that
    keeps a new ``sqlalchemy.*`` logger quiet until an explicit policy exists);
    every other logger keeps NOTSET so it inherits from root (caplog works).
    """
    for noisy in NOISY_LOGGERS:
        if name == noisy or name.startswith(noisy + "."):
            return logging.WARNING
    return logging.NOTSET


@dataclass
class LevelOverride:
    """One persisted override: a canonical level and an absolute UTC expiry."""

    level: str
    expires_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now


class RuntimeLevelPolicy:
    """Resolve categories to logger levels and apply them (D-15/D-17/D-22).

    ``env`` is the parsed process basis, ``snapshot`` the active DB overrides.
    The clock seam ``utc_now`` is injectable so tests never sleep.
    """

    def __init__(
        self,
        env: EnvLevelPolicy,
        *,
        foundation_levels: Mapping[str, int] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.env = env
        self.snapshot: dict[str, LevelOverride] = {}
        self.foundation_levels: dict[str, int] = dict(
            foundation_levels
            if foundation_levels is not None
            else build_foundation_levels()
        )
        self._baseline: dict[str, int] = {}
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))

    # ── clock seam ────────────────────────────────────────────────────────
    def now(self) -> datetime:
        return self._utc_now()

    # ── basis / threshold ─────────────────────────────────────────────────
    def base_level(self, category: str) -> str:
        """The env basis of a category: env category > explicit global > INFO."""
        if category in self.env.category_levels:
            return self.env.category_levels[category]
        if self.env.global_explicit:
            return self.env.global_level  # type: ignore[return-value]
        return "INFO"

    def ttl_threshold(self, category: str) -> int:
        """D-22: the level below which an override MUST carry an expiry.

        It is the highest resting level of the category's real members under
        the pure env/foundation policy (never the currently applied override):

        * an explicit ``LOG_LEVELS[category]`` entry already sets every member,
          so the basis is the member level;
        * a bare explicit ``LOG_LEVEL`` may only RAISE noisy members, so the
          threshold is ``max(foundation_member, global)``;
        * without any explicit policy the Foundation noisy WARNING governs.
        """
        basis = LEVEL_VALUES[self.base_level(category)]
        if category in self.env.category_levels:
            return basis
        if self.env.global_explicit:
            if category in _NOISY_CATEGORIES:
                return max(
                    self.foundation_levels.get(category, logging.NOTSET), basis
                )
            return basis
        return max(basis, self.foundation_levels.get(category, logging.NOTSET))

    # ── snapshot ──────────────────────────────────────────────────────────
    def active_override(self, category: str) -> LevelOverride | None:
        override = self.snapshot.get(category)
        if override is None or not override.is_active(self.now()):
            return None
        return override

    def apply_snapshot(self, snapshot: Mapping[str, LevelOverride]) -> None:
        self.snapshot = dict(snapshot)
        self.apply_all()

    def restore_foundation(self) -> None:
        self.snapshot = {}
        self.apply_all()

    def set_foundation_levels(self, levels: Mapping[str, int]) -> None:
        self.foundation_levels = dict(levels)

    # ── logger application ────────────────────────────────────────────────
    def capture_baseline(self) -> None:
        """Record the pre-policy explicit level of every current logger once."""
        manager = logging.Logger.manager
        for name, obj in list(manager.loggerDict.items()):
            if isinstance(obj, logging.Logger):
                self._baseline.setdefault(name, obj.level)

    def _baseline_for(self, name: str) -> int:
        if name in self._baseline:
            return self._baseline[name]
        return _foundation_level_for_logger(name)

    def _target_for(self, name: str) -> int | None:
        category = category_for(name)
        override = self.active_override(category)
        if override is not None:
            return LEVEL_VALUES[override.level]
        if category in self.env.category_levels:
            return LEVEL_VALUES[self.env.category_levels[category]]
        if self.env.global_explicit:
            global_value = LEVEL_VALUES[self.env.global_level]  # type: ignore[index]
            if category in _NOISY_CATEGORIES:
                # D-22: a global LOG_LEVEL may only raise noisy members.
                return max(self.foundation_levels.get(category, logging.NOTSET), global_value)
            return global_value
        return None

    def apply_logger(self, target_logger: logging.Logger) -> None:
        """Set one logger's level to the policy target (or restore its baseline)."""
        name = target_logger.name
        target = self._target_for(name)
        desired = self._baseline_for(name) if target is None else target
        if target_logger.level != desired:
            target_logger.setLevel(desired)

    def apply_all(self) -> None:
        """Apply the policy to every already-existing logger (never create one)."""
        manager = logging.Logger.manager
        for name in list(manager.loggerDict):
            obj = manager.loggerDict[name]
            if isinstance(obj, logging.Logger):
                self.apply_logger(obj)


# ── installation seam ─────────────────────────────────────────────────────

_POLICY: RuntimeLevelPolicy | None = None
_BASE_LOGGER_CLASS: type[logging.Logger] | None = None


class _PolicyLogger(logging.Logger):
    """A Logger that applies the active policy the instant it is created.

    The tap runs synchronously after ``super().__init__`` and performs no I/O,
    so a logger created inside the log hot path still gets the right level
    before its first emit (research Pattern 2).
    """

    def __init__(self, name: str, level: int = logging.NOTSET) -> None:
        super().__init__(name, level)
        policy = _POLICY
        if policy is not None:
            try:
                policy.apply_logger(self)
            except Exception:  # pragma: no cover - never break logger creation
                pass


def get_installed_policy() -> RuntimeLevelPolicy | None:
    return _POLICY


def install_level_policy(env: EnvLevelPolicy | None = None) -> RuntimeLevelPolicy:
    """Install (once) or refresh the process-wide level policy.

    Idempotent: the first call captures the Foundation baseline and swaps in
    the policy logger class; later calls only refresh the env basis and
    re-apply, so an active DB snapshot survives a repeated ``configure_logging``.
    """
    global _POLICY, _BASE_LOGGER_CLASS
    if _POLICY is None:
        _POLICY = RuntimeLevelPolicy(
            env if env is not None else parse_env_levels(),
            foundation_levels=build_foundation_levels(),
        )
        _POLICY.capture_baseline()
        _BASE_LOGGER_CLASS = logging.getLoggerClass()
    elif env is not None:
        _POLICY.env = env
    logging.setLoggerClass(_PolicyLogger)
    _POLICY.apply_all()
    return _POLICY


def reset_level_policy() -> None:
    """Restore the pre-299 logger class and drop the policy (tests, shutdown)."""
    global _POLICY, _BASE_LOGGER_CLASS
    if _POLICY is not None:
        _POLICY.restore_foundation()
    _POLICY = None
    if _BASE_LOGGER_CLASS is not None:
        logging.setLoggerClass(_BASE_LOGGER_CLASS)
        _BASE_LOGGER_CLASS = None


# ── snapshot (de)serialization ─────────────────────────────────────────────

def serialize_snapshot(snapshot: Mapping[str, LevelOverride]) -> str:
    """Canonical JSON for the reserved key: sorted categories, absolute UTC."""
    payload: dict[str, dict[str, Any]] = {}
    for category in sorted(snapshot):
        override = snapshot[category]
        payload[category] = {
            "level": override.level,
            "expires_at": (
                override.expires_at.astimezone(timezone.utc).isoformat()
                if override.expires_at is not None
                else None
            ),
        }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_snapshot(raw: str | None) -> dict[str, LevelOverride]:
    """Parse a persisted value into overrides, raising on any malformed shape."""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidLevelSnapshot("stored logging.levels is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InvalidLevelSnapshot("stored logging.levels is not a JSON object")

    snapshot: dict[str, LevelOverride] = {}
    for category, entry in data.items():
        if not is_valid_category(category):
            raise InvalidLevelSnapshot(f"stored category {category!r} is invalid")
        if not isinstance(entry, dict):
            raise InvalidLevelSnapshot(f"stored entry for {category!r} is not an object")
        if set(entry) - {"level", "expires_at"}:
            raise InvalidLevelSnapshot(f"stored entry for {category!r} has unknown fields")
        try:
            level = normalize_level(entry.get("level"))
        except InvalidLevelError as exc:
            raise InvalidLevelSnapshot(f"stored level for {category!r} is invalid") from exc
        raw_expiry = entry.get("expires_at")
        if raw_expiry is None:
            expires_at = None
        else:
            if not isinstance(raw_expiry, str):
                raise InvalidLevelSnapshot(f"stored expiry for {category!r} is invalid")
            try:
                parsed = datetime.fromisoformat(raw_expiry)
            except ValueError as exc:
                raise InvalidLevelSnapshot(f"stored expiry for {category!r} is invalid") from exc
            if parsed.tzinfo is None:
                raise InvalidLevelSnapshot(f"stored expiry for {category!r} is naive")
            expires_at = parsed.astimezone(timezone.utc)
        snapshot[category] = LevelOverride(level=level, expires_at=expires_at)
    return snapshot


def snapshot_value(override: LevelOverride | None) -> dict[str, Any] | None:
    """The audit/security projection of one override (never ``ttl_seconds``)."""
    if override is None:
        return None
    return {
        "level": override.level,
        "expires_at": (
            override.expires_at.astimezone(timezone.utc).isoformat()
            if override.expires_at is not None
            else None
        ),
    }


def order_categories(categories: Iterable[str]) -> list[str]:
    """Core categories first, then connector/plugin groups alphabetically."""
    values = set(categories)
    core = [category for category in CORE_CATEGORY_ORDER if category in values]
    connectors = sorted(c for c in values if c.startswith("connector."))
    plugins = sorted(c for c in values if c.startswith("plugin."))
    others = sorted(
        c
        for c in values
        if c not in core and not c.startswith("connector.") and not c.startswith("plugin.")
    )
    return core + connectors + plugins + others


# ── LevelController ────────────────────────────────────────────────────────

class LevelController:
    """Per-process poller: expiry, new loggers, version signal and DB reconcile.

    One instance per process, created outside any request/job context. Every
    tick evaluates the known snapshot against UTC *before* any I/O, then reads
    the version signal (bounded) and falls back to an independent DB reconcile
    (D-07/D-14/D-16, research Pattern 8).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        policy: RuntimeLevelPolicy | None = None,
        version_key: str = LEVEL_VERSION_KEY,
        interval: float = 5.0,
        reconcile_interval: float = 60.0,
        io_timeout: float = 1.0,
        utc_now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
        read_version: Callable[[], Any] | None = None,
        publish_version: Callable[[], Any] | None = None,
        discovery: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.policy = policy or _POLICY or RuntimeLevelPolicy(parse_env_levels())
        # Plan-02 process discovery (D-20): reports real dynamic categories to
        # the central offer. Optional so the storage/alembic/test controllers
        # keep working without a lease.
        self.discovery = discovery
        self.version_key = version_key
        self.interval = interval
        self.reconcile_interval = reconcile_interval
        self.io_timeout = io_timeout
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_now or time.monotonic
        self._read_version_hook = read_version
        self._publish_version_hook = publish_version
        self._task: asyncio.Task | None = None
        self.apply_lock = asyncio.Lock()
        self.seen_version: str | None = None
        self.last_reconcile: float = 0.0
        self.snapshot: dict[str, LevelOverride] = {}
        self.last_error: str | None = None

    def now(self) -> datetime:
        return self._utc_now()

    def report_local_categories(self) -> list[Any]:
        """The real dynamic categories this process reports (empty when absent)."""
        if self.discovery is None:
            return []
        collect = getattr(self.discovery, "collect_local", None)
        if collect is None:
            return []
        return collect()

    async def start(self) -> None:
        await self.refresh_from_db()
        self.last_reconcile = self._monotonic()
        await self._discovery_heartbeat(force=True)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._discovery_stop()

    async def _discovery_heartbeat(self, *, force: bool = False) -> None:
        """Independent, bounded discovery report — never breaks level polling."""
        if self.discovery is None:
            return
        heartbeat = getattr(self.discovery, "heartbeat", None)
        if heartbeat is None:
            return
        try:
            await asyncio.wait_for(heartbeat(force=force), self.io_timeout)
        except Exception:
            logger.warning("log level discovery heartbeat failed", exc_info=True)

    async def _discovery_cleanup(self) -> None:
        if self.discovery is None:
            return
        cleanup = getattr(self.discovery, "cleanup_expired", None)
        if cleanup is None:
            return
        try:
            await asyncio.wait_for(cleanup(), self.io_timeout)
        except Exception:
            logger.warning("log level discovery cleanup failed", exc_info=True)

    async def _discovery_stop(self) -> None:
        if self.discovery is None:
            return
        stop = getattr(self.discovery, "stop", None)
        if stop is None:
            return
        try:
            await asyncio.wait_for(stop(), self.io_timeout)
        except Exception:
            logger.warning("log level discovery stop failed", exc_info=True)

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("log level controller tick failed", exc_info=True)

    async def tick(self) -> None:
        # 1) Expiry + new-logger scan happen on EVERY tick, regardless of I/O.
        async with self.apply_lock:
            self.policy.snapshot = dict(self.snapshot)
            self.policy.apply_all()

        # 2) Version signal, then an independent DB reconcile when due.
        reconcile_due = (
            self._monotonic() - self.last_reconcile
        ) >= self.reconcile_interval
        version: str | None = None
        try:
            version = await asyncio.wait_for(self._read_version(), self.io_timeout)
        except Exception:
            version = None

        if reconcile_due or version is None or version != self.seen_version:
            await self.refresh_from_db(version)

        # 3) Discovery heartbeat/cleanup are a SEPARATE, independently caught
        #    step: a discovery failure must never cancel the level reconcile or
        #    the next version-signal check (D-20, plan 299-02).
        await self._discovery_heartbeat()
        await self._discovery_cleanup()

    async def _read_version(self) -> str | None:
        if self._read_version_hook is not None:
            return await self._read_version_hook()
        from app.db.redis import get_redis

        redis = await get_redis()
        value = await redis.get(self.version_key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def refresh_from_db(self, version: str | None = None) -> None:
        """Read, validate and apply the latest DB snapshot under the apply lock."""
        from app.services.logging_service import read_level_snapshot

        if self.session_factory is None:
            return
        async with self.apply_lock:
            try:
                snapshot = await read_level_snapshot(self.session_factory)
            except Exception:
                # Fail-static: keep the last known snapshot on any read/parse error.
                self.last_error = "snapshot_unavailable"
                logger.warning(
                    "log level DB reconcile failed; keeping last snapshot",
                    exc_info=True,
                )
                return
            self.snapshot = snapshot
            self.policy.snapshot = dict(snapshot)
            self.policy.apply_all()
            self.last_reconcile = self._monotonic()
            self.last_error = None
        if version is not None:
            self.seen_version = version


__all__ = [
    "CORE_CATEGORIES",
    "CORE_CATEGORY_ORDER",
    "DISCOVERY_KEY_PREFIX",
    "EnvLevelPolicy",
    "InvalidLevelError",
    "InvalidLevelSnapshot",
    "LEVEL_NAMES",
    "LEVEL_VALUES",
    "LEVEL_VERSION_KEY",
    "LevelController",
    "LevelOverride",
    "LOGGING_LEVELS_KEY",
    "MAX_BODY_BYTES",
    "MAX_CATEGORIES_PER_REQUEST",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "RuntimeLevelPolicy",
    "build_foundation_levels",
    "get_installed_policy",
    "install_level_policy",
    "normalize_category",
    "normalize_level",
    "order_categories",
    "parse_env_levels",
    "parse_snapshot",
    "reset_level_policy",
    "serialize_snapshot",
    "snapshot_value",
]
