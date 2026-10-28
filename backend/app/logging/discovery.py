"""Cross-process log-category discovery via bounded DB leases (D-20, plan 299-02).

The level API's GET/PUT offer must include categories that only exist in *other*
long-lived processes (notably the ARQ worker). Rather than a synthetic catalog
built from installed plugin names, each process reports the category set it can
actually observe from real ``logging.Logger`` instances. The report is a short
lived row in the existing ``system_settings`` table under a reserved internal
prefix — no new table, no second level key, and the ``logging.levels`` JSON
shape is untouched.

DB stays the truth; Valkey stays a pure invalidation signal. This module owns:

* the report DTO and its strict (de)serialization,
* a per-process :class:`LoggingCategoryDiscovery` that heartbeats its real
  logger projection with a lease derived from PostgreSQL time and deletes only
  its own row on shutdown,
* a reader that validates live reports and fails explicitly (never a truncated
  success) when the projection cannot be produced.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import delete, select, text

from app.logging.categories import CATEGORY_RE, category_for
from app.logging.levels import DISCOVERY_KEY_PREFIX

logger = logging.getLogger(__name__)

# Reserved internal prefix (kept identical to the generic-write guard's constant).
DISCOVERY_PREFIX = DISCOVERY_KEY_PREFIX

# A report is alive for 30s; a process refreshes its own lease once the
# remaining lifetime drops below 20s (≈one write per 10s instead of per 5s).
DISCOVERY_LEASE_SECONDS = 30
DISCOVERY_REFRESH_BELOW_SECONDS = 20

# Resource guards: never a synthetic category, only a bounded real projection.
MAX_DISCOVERY_REPORTS = 128
MAX_DISCOVERY_CATEGORIES = 1024
MAX_DISCOVERY_REPORT_BYTES = 65536
MAX_DISCOVERY_CLEANUP = 64

VALID_ROLES = ("backend", "worker")

# Mirrors ``app.plugin_system.connector_registry.CORE_OWNER`` without importing
# the plugin package at module scope (this module is importable by the backend
# and worker only, but a leaf import keeps the logging package lightweight).
CORE_OWNER = "core"


class LoggingDiscoveryUnavailable(RuntimeError):
    """The process-discovery projection could not be produced (→ HTTP 503)."""


class InvalidDiscoveryReport(ValueError):
    """A persisted discovery row is not a valid report."""


@dataclass(frozen=True)
class DiscoveryCategory:
    """One real, process-local category and the owner that legitimizes it."""

    category: str
    owner: str


@dataclass
class DiscoveryReport:
    """A process's observed dynamic-category projection and its lease."""

    process_id: str
    role: str
    expires_at: datetime
    categories: list[DiscoveryCategory] = field(default_factory=list)
    unavailable: bool = False
    role_label: str | None = None

    def category_names(self) -> list[str]:
        return [entry.category for entry in self.categories]


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def report_to_json(report: DiscoveryReport) -> str:
    """Canonical report JSON: sorted categories, absolute UTC expiry."""
    payload = {
        "process_id": report.process_id,
        "role": report.role,
        "expires_at": _as_aware(report.expires_at).astimezone(timezone.utc).isoformat(),
        "categories": [
            {"category": entry.category, "owner": entry.owner}
            for entry in report.categories
        ],
        "unavailable": bool(report.unavailable),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _parse_report_shape(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidDiscoveryReport("discovery value is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InvalidDiscoveryReport("discovery value is not a JSON object")
    return data


def parse_report(key: str, raw: str | None) -> DiscoveryReport:
    """Validate a persisted discovery row completely or raise."""
    if raw is None:
        raise InvalidDiscoveryReport(f"discovery row {key!r} has no value")
    data = _parse_report_shape(raw)

    process_id = data.get("process_id")
    if not isinstance(process_id, str) or not process_id:
        raise InvalidDiscoveryReport(f"discovery row {key!r} has no process_id")
    role = data.get("role")
    if role not in VALID_ROLES:
        raise InvalidDiscoveryReport(f"discovery row {key!r} has an invalid role")
    raw_expiry = data.get("expires_at")
    if not isinstance(raw_expiry, str):
        raise InvalidDiscoveryReport(f"discovery row {key!r} has no expires_at")
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError as exc:
        raise InvalidDiscoveryReport(f"discovery row {key!r} has an invalid expiry") from exc
    if expires_at.tzinfo is None:
        raise InvalidDiscoveryReport(f"discovery row {key!r} has a naive expiry")
    expires_at = expires_at.astimezone(timezone.utc)

    raw_categories = data.get("categories", [])
    if not isinstance(raw_categories, list):
        raise InvalidDiscoveryReport(f"discovery row {key!r} categories is not a list")
    if len(raw_categories) > MAX_DISCOVERY_CATEGORIES:
        raise InvalidDiscoveryReport(f"discovery row {key!r} exceeds the category limit")
    categories: list[DiscoveryCategory] = []
    seen: set[str] = set()
    for entry in raw_categories:
        if not isinstance(entry, dict):
            raise InvalidDiscoveryReport(f"discovery row {key!r} has a non-object category")
        category = entry.get("category")
        owner = entry.get("owner")
        if (
            not isinstance(category, str)
            or not CATEGORY_RE.fullmatch(category)
            or not isinstance(owner, str)
            or not owner
            or category in seen
        ):
            raise InvalidDiscoveryReport(f"discovery row {key!r} has an invalid category")
        seen.add(category)
        categories.append(DiscoveryCategory(category=category, owner=owner))

    unavailable = data.get("unavailable", False)
    if not isinstance(unavailable, bool):
        raise InvalidDiscoveryReport(f"discovery row {key!r} has a non-bool unavailable")
    if unavailable and categories:
        raise InvalidDiscoveryReport(
            f"discovery row {key!r} is unavailable but carries categories"
        )

    if set(data) - {"process_id", "role", "expires_at", "categories", "unavailable"}:
        raise InvalidDiscoveryReport(f"discovery row {key!r} has unknown fields")

    return DiscoveryReport(
        process_id=process_id,
        role=role,
        expires_at=expires_at,
        categories=categories,
        unavailable=unavailable,
    )


def _lenient_expiry(raw: str | None) -> datetime | None:
    """Best-effort expiry extraction for cleanup of malformed/dead rows."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_expiry = data.get("expires_at")
    if not isinstance(raw_expiry, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_expiry)
    except ValueError:
        return None
    return _as_aware(parsed).astimezone(timezone.utc)


def _dead_discovery_row(key: str, value: str | None, db_now: datetime) -> bool:
    """True when a ``logging.discovery:`` row can never offer a live category.

    Covers both a readable-but-expired expiry and a malformed row whose expiry
    cannot be read at all. The latter was previously deletable by no code path
    and permanently wedged the level API with 503 (WR-01).
    """
    expiry = _lenient_expiry(value)
    if expiry is not None:
        return expiry <= db_now
    try:
        parse_report(key, value)
    except InvalidDiscoveryReport:
        return True
    return False


def owner_for_category(
    category: str, *, resolver: Callable[[str], Any] | None = None
) -> str | None:
    """The owner string for a dynamic category, or ``None`` when unprovable.

    ``plugin.<name>`` is owned by the manifest ``<name>`` already resolved by
    ``category_for``. ``connector.<name>`` is owned by the connector-type
    registry declaration — never guessed from the logger name.
    """
    if category.startswith("plugin."):
        name = category[len("plugin."):]
        return name or None
    if category.startswith("connector."):
        name = category[len("connector."):]
        if not name:
            return None
        from app.plugin_system.connector_registry import (
            CORE_OWNER as _CORE_OWNER,
            get_connector_type_registry,
        )

        if resolver is not None:
            entry = resolver(name)
        else:
            entry = get_connector_type_registry().resolve(name)
        if entry is None:
            return None
        return getattr(entry, "owner", None) or _CORE_OWNER
    return None


class LoggingCategoryDiscovery:
    """Per-process discovery reporter over bounded ``system_settings`` leases."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        role: str = "backend",
        process_id: str | None = None,
        connector_resolver: Callable[[str], Any] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"unknown discovery role {role!r}")
        self.session_factory = session_factory
        self.role = role
        self.process_id = str(process_id or uuid.uuid4())
        self._connector_resolver = connector_resolver
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._last_categories: list[DiscoveryCategory] | None = None
        self.last_error: str | None = None

    # ── identity ──────────────────────────────────────────────────────────
    @property
    def key(self) -> str:
        return f"{DISCOVERY_PREFIX}{self.process_id}"

    def now(self) -> datetime:
        return self._utc_now()

    # ── local projection ──────────────────────────────────────────────────
    def collect_local(self) -> list[DiscoveryCategory]:
        """Real dynamic (connector./plugin.) categories in this process."""
        found: dict[str, DiscoveryCategory] = {}
        manager = logging.Logger.manager
        for name, obj in list(manager.loggerDict.items()):
            if not isinstance(obj, logging.Logger):
                continue
            category = category_for(name)
            if not CATEGORY_RE.fullmatch(category):
                continue
            if not (
                category.startswith("connector.") or category.startswith("plugin.")
            ):
                continue
            if category in found:
                continue
            owner = owner_for_category(
                category, resolver=self._connector_resolver
            )
            if owner is None:
                # An unprovable owner (unregistered connector) is never offered.
                continue
            found[category] = DiscoveryCategory(category=category, owner=owner)
        return [found[key] for key in sorted(found)]

    # ── heartbeat ─────────────────────────────────────────────────────────
    def _build_report(
        self, categories: list[DiscoveryCategory], db_now: datetime
    ) -> DiscoveryReport:
        expires_at = db_now + timedelta(seconds=DISCOVERY_LEASE_SECONDS)
        if len(categories) > MAX_DISCOVERY_CATEGORIES:
            return DiscoveryReport(
                process_id=self.process_id,
                role=self.role,
                expires_at=expires_at,
                categories=[],
                unavailable=True,
            )
        report = DiscoveryReport(
            process_id=self.process_id,
            role=self.role,
            expires_at=expires_at,
            categories=categories,
        )
        encoded = report_to_json(report)
        if len(encoded.encode("utf-8")) > MAX_DISCOVERY_REPORT_BYTES:
            return DiscoveryReport(
                process_id=self.process_id,
                role=self.role,
                expires_at=expires_at,
                categories=[],
                unavailable=True,
            )
        return report

    async def _db_now(self, db) -> datetime:
        value = (await db.execute(text("SELECT now()"))).scalar_one()
        return _as_aware(value)

    async def heartbeat(self, *, force: bool = False) -> bool:
        """Upsert this process's report when its projection changed or the lease
        is nearly due. Returns ``True`` when a write happened. Fail-soft: any
        I/O error is recorded and swallowed (the level poll must keep running).
        """
        if self.session_factory is None:
            return False
        try:
            categories = self.collect_local()
            async with self.session_factory() as db:
                db_now = await self._db_now(db)
                existing_raw = (
                    await db.execute(
                        select(SystemSetting.value).where(
                            SystemSetting.key == self.key
                        )
                    )
                ).scalar_one_or_none()
                if existing_raw is not None and not force:
                    try:
                        existing = parse_report(self.key, existing_raw)
                    except InvalidDiscoveryReport:
                        existing = None
                    if (
                        existing is not None
                        and not existing.unavailable
                        and existing.categories == categories
                        and existing.expires_at - db_now
                        > timedelta(seconds=DISCOVERY_REFRESH_BELOW_SECONDS)
                    ):
                        self.last_error = None
                        return False

                report = self._build_report(categories, db_now)
                value = report_to_json(report)
                existing = (
                    await db.execute(
                        select(SystemSetting).where(SystemSetting.key == self.key)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(SystemSetting(key=self.key, value=value))
                else:
                    existing.value = value
                await db.commit()
            self._last_categories = categories
            self.last_error = None
            return True
        except Exception:
            self.last_error = "heartbeat_failed"
            logger.warning("logging discovery heartbeat failed", exc_info=True)
            return False

    # ── reading (central resolver) ────────────────────────────────────────
    async def read_active(self) -> list[DiscoveryReport]:
        """Live reports (``expires_at > DB now``), strictly validated.

        Malformed *live* rows and any overflow fail explicitly with
        :class:`LoggingDiscoveryUnavailable` — never a truncated success.
        """
        if self.session_factory is None:
            return []
        async with self.session_factory() as db:
            db_now = await self._db_now(db)
            rows = (
                await db.execute(
                    select(SystemSetting.key, SystemSetting.value)
                    .where(SystemSetting.key.like(DISCOVERY_PREFIX + "%"))
                    .limit(MAX_DISCOVERY_REPORTS + 1)
                )
            ).all()

        live: list[DiscoveryReport] = []
        for key, value in rows:
            try:
                report = parse_report(key, value)
            except InvalidDiscoveryReport:
                expiry = _lenient_expiry(value)
                if expiry is not None and expiry > db_now:
                    # A malformed *live* row fails explicit (D-07); its readable
                    # future expiry proves the row claims to be current.
                    raise LoggingDiscoveryUnavailable(
                        "a live discovery report is malformed"
                    )
                # A readable-but-dead expiry and a missing/unparseable expiry
                # are both treated as dead: one such row must never wedge the
                # level API with a permanent 503 (WR-01).
                continue
            if report.expires_at <= db_now:
                continue
            if report.unavailable:
                raise LoggingDiscoveryUnavailable(
                    "a process reported an incomplete discovery projection"
                )
            live.append(report)
        if len(live) > MAX_DISCOVERY_REPORTS:
            raise LoggingDiscoveryUnavailable("too many live discovery reports")
        return live

    # ── maintenance ───────────────────────────────────────────────────────
    async def cleanup_expired(self) -> int:
        """Delete at most ``MAX_DISCOVERY_CLEANUP`` expired prefix rows."""
        if self.session_factory is None:
            return 0
        try:
            async with self.session_factory() as db:
                db_now = await self._db_now(db)
                rows = (
                    await db.execute(
                        select(SystemSetting.key, SystemSetting.value)
                        .where(SystemSetting.key.like(DISCOVERY_PREFIX + "%"))
                        .limit(MAX_DISCOVERY_CLEANUP * 4)
                    )
                ).all()
                expired = [
                    key
                    for key, value in rows
                    if _dead_discovery_row(key, value, db_now)
                ][:MAX_DISCOVERY_CLEANUP]
                for key in expired:
                    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
                await db.commit()
                return len(expired)
        except Exception:
            logger.debug("logging discovery cleanup failed", exc_info=True)
            return 0

    async def stop(self) -> None:
        """Remove only this process's own lease row (best effort)."""
        if self.session_factory is None:
            return
        try:
            async with self.session_factory() as db:
                await db.execute(delete(SystemSetting).where(SystemSetting.key == self.key))
                await db.commit()
        except Exception:
            logger.debug("logging discovery stop failed", exc_info=True)

    # ── test seam ─────────────────────────────────────────────────────────
    def _utcnow(self) -> datetime:  # pragma: no cover - introspection aid
        return self.now()

    async def _cleanup_expired_now(self) -> int:  # pragma: no cover - introspection aid
        return await self.cleanup_expired()


def discovery_prefix_filter(keys: Iterable[str]) -> list[str]:
    """Drop reserved discovery keys from a generic key listing."""
    return [key for key in keys if not key.startswith(DISCOVERY_PREFIX)]


# Imported at the bottom to keep the module's public surface tidy while avoiding
# an import cycle (``SystemSetting`` imports nothing from ``app.logging``).
from app.models.system_setting import SystemSetting  # noqa: E402


__all__ = [
    "CORE_OWNER",
    "DISCOVERY_LEASE_SECONDS",
    "DISCOVERY_PREFIX",
    "DISCOVERY_REFRESH_BELOW_SECONDS",
    "DiscoveryCategory",
    "DiscoveryReport",
    "InvalidDiscoveryReport",
    "LoggingCategoryDiscovery",
    "LoggingDiscoveryUnavailable",
    "MAX_DISCOVERY_CATEGORIES",
    "MAX_DISCOVERY_CLEANUP",
    "MAX_DISCOVERY_REPORTS",
    "MAX_DISCOVERY_REPORT_BYTES",
    "discovery_prefix_filter",
    "owner_for_category",
    "parse_report",
    "report_to_json",
]
