"""Fail-closed Fernet key-rotation pass for the maintenance CLI (SEC313-69).

Phase 317-06 made a rotation possible by keeping ``FERNET_KEY_OLD`` as a second
key; this module finishes it. Every stored ``v1:`` ciphertext that only the
previous key can read is re-encrypted under ``FERNET_KEY``, and the run only
reports that the old key can be removed once a fresh session proves every
stored value decrypts with ``FERNET_KEY`` alone.

Ciphertext is read and written as raw strings on purpose. Reading it through the
encrypted column type would turn an undecryptable value into the invalid-token
placeholder, and a write-back would then store that placeholder as ciphertext.

Ciphertext the Core does not model — a third-party plugin's own ``plugin_*``
table — is never rewritten: an inventory sweep reports it, and any hit blocks
the verdict.

Exit codes: ``0`` safe, ``1`` not safe (locations listed), ``2`` the run failed
and was rolled back.
"""

from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text, cast, column, literal, select, table, text

from app.crypto import VERSION_PREFIX, init_fernet

STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_UNREADABLE = "unreadable"
STATUS_PLAINTEXT = "plaintext"

EXIT_SAFE = 0
EXIT_NOT_SAFE = 1

SAFE_VERDICT = "FERNET_KEY_OLD can be removed"

_PREFIX = VERSION_PREFIX.decode("ascii")

_SERVICE_ACCOUNT_SELECT = (
    "SELECT id, source_bearer_token, target_bearer_token FROM service_accounts"
)
_SERVICE_ACCOUNT_SCOPE = " WHERE id = ANY(CAST(:ids AS uuid[]))"

_PLUGIN_SELECT = "SELECT id, name, config::text AS config FROM plugins"
_PLUGIN_SCOPE = " WHERE id = ANY(CAST(:ids AS uuid[]))"

_LOCK_SUFFIX = " FOR UPDATE"

_UPDATE_SOURCE_TOKEN = text(
    "UPDATE service_accounts SET source_bearer_token = :value WHERE id = :id"
)
_UPDATE_TARGET_TOKEN = text(
    "UPDATE service_accounts SET target_bearer_token = :value WHERE id = :id"
)
_UPDATE_PLUGIN_CONFIG = text(
    "UPDATE plugins SET config = CAST(:config AS jsonb) WHERE id = :id"
)

_TOKEN_COLUMNS = (
    ("source_bearer_token", _UPDATE_SOURCE_TOKEN),
    ("target_bearer_token", _UPDATE_TARGET_TOKEN),
)

_PLUGIN_TABLE_COLUMNS = text(
    "SELECT c.table_name, c.column_name "
    "FROM information_schema.columns c "
    "JOIN information_schema.tables t "
    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
    "WHERE c.table_schema = 'public' "
    "AND t.table_type = 'BASE TABLE' "
    "AND c.table_name LIKE 'plugin\\_%' "
    "AND c.data_type IN ('text', 'character varying', 'json', 'jsonb') "
    "ORDER BY c.table_name, c.column_name"
)

_CIPHERTEXT_PROBE = "%v1:gAAAAA%"


class RotationError(RuntimeError):
    """A failed rotation stage, carrying no driver text (it can embed values)."""


def load_keys() -> tuple[Fernet, MultiFernet, bool]:
    """Initialize the process keys and report whether an old key is configured.

    ``init_fernet()`` is called here because a bare maintenance process has no
    lifespan (317-06 saw "Fernet not initialized" from exactly that); an invalid
    key raises, which ends the run with the failure exit code.
    """
    multi = init_fernet()
    key = os.environ["FERNET_KEY"]
    primary = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    return primary, multi, bool(os.environ.get("FERNET_KEY_OLD"))


def classify_value(stored: str, primary: Fernet, multi: MultiFernet) -> str:
    """Classify one stored value: plaintext, current, stale or unreadable."""
    if not isinstance(stored, str) or not stored.startswith(_PREFIX):
        return STATUS_PLAINTEXT
    body = stored[len(_PREFIX):].encode("utf-8")
    try:
        primary.decrypt(body)
        return STATUS_CURRENT
    except InvalidToken:
        pass
    try:
        multi.decrypt(body)
        return STATUS_STALE
    except InvalidToken:
        return STATUS_UNREADABLE


def rotate_value(stored: str, multi: MultiFernet) -> str:
    """Re-encrypt a stale value under the primary key (call only for ``stale``)."""
    body = stored[len(_PREFIX):].encode("utf-8")
    return _PREFIX + multi.rotate(body).decode("utf-8")


def walk_encrypted_strings(document, visit, path: str = "$") -> tuple[object, bool]:
    """Visit every versioned string in a JSON document, recursively.

    ``visit(path, value)`` returns a replacement or ``None`` to leave the value
    alone. Paths use ``$.key`` and ``[index]`` notation. Returns the (possibly
    new) document and whether anything changed.
    """
    changed = False

    def _walk(node, current_path: str):
        nonlocal changed
        if isinstance(node, dict):
            return {
                key: _walk(value, f"{current_path}.{key}")
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [
                _walk(value, f"{current_path}[{index}]")
                for index, value in enumerate(node)
            ]
        if isinstance(node, str) and node.startswith(_PREFIX):
            replacement = visit(current_path, node)
            if replacement is not None and replacement != node:
                changed = True
                return replacement
        return node

    return _walk(document, path), changed


def _empty_counts() -> dict:
    return {
        "scanned": 0,
        "current": 0,
        "stale": 0,
        "rotated": 0,
        "unreadable": 0,
        "plaintext": 0,
    }


def _skipped_counts() -> dict:
    counts = _empty_counts()
    counts["skipped"] = True
    return counts


def _service_account_select_sql(*, scoped: bool, lock: bool):
    """One of four fixed statements — no identifier ever comes from data."""
    return text(
        _SERVICE_ACCOUNT_SELECT
        + (_SERVICE_ACCOUNT_SCOPE if scoped else "")
        + (_LOCK_SUFFIX if lock else "")
    )


def _plugin_select_sql(*, scoped: bool, lock: bool):
    """The plugin-config equivalent: fixed text, bound scope, dialect-quoted lock."""
    return text(
        _PLUGIN_SELECT
        + (_PLUGIN_SCOPE if scoped else "")
        + (_LOCK_SUFFIX if lock else "")
    )


async def process_service_accounts(
    session, *, primary: Fernet, multi: MultiFernet, check: bool, ids=None
) -> dict:
    """Classify both token columns and rewrite every stale value in place.

    ``ids`` scopes the pass to explicit rows (the tests' only mode; the CLI
    passes none). Values that already decrypt with the primary key are never
    rewritten — ``MultiFernet.rotate`` is not idempotent.
    """
    counts = _empty_counts()
    locations: list[dict] = []
    scoped = ids is not None
    params = {"ids": [str(row_id) for row_id in ids]} if scoped else {}

    result = await session.execute(
        _service_account_select_sql(scoped=scoped, lock=not check), params
    )
    for row in result.all():
        for column_name, update_statement in _TOKEN_COLUMNS:
            stored = getattr(row, column_name)
            if stored is None:
                continue
            counts["scanned"] += 1
            status = classify_value(stored, primary, multi)
            counts[status] += 1
            if status == STATUS_STALE and not check:
                await session.execute(
                    update_statement,
                    {"value": rotate_value(stored, multi), "id": row.id},
                )
                counts["rotated"] += 1
                continue
            if status != STATUS_CURRENT:
                locations.append(
                    {
                        "store": "service_accounts",
                        "id": str(row.id),
                        "column": column_name,
                        "status": status,
                    }
                )
    return {"counts": counts, "locations": locations}


async def process_plugin_configs(
    session, *, primary: Fernet, multi: MultiFernet, check: bool, ids=None
) -> dict:
    """Classify every versioned string inside each plugin's JSONB config.

    Every prefixed string counts, not only the manifest's secret paths — a
    maintenance process does not load plugin manifests. A non-secret string that
    merely starts with the prefix fails both decryptions and is reported
    ``unreadable`` (fail-closed, operator decides). The document is written back
    only when a value changed.
    """
    counts = _empty_counts()
    locations: list[dict] = []
    scoped = ids is not None
    params = {"ids": [str(row_id) for row_id in ids]} if scoped else {}

    result = await session.execute(
        _plugin_select_sql(scoped=scoped, lock=not check), params
    )
    for row in result.all():
        document = json.loads(row.config)

        def _visit(path: str, value: str, row=row) -> str | None:
            counts["scanned"] += 1
            status = classify_value(value, primary, multi)
            counts[status] += 1
            if status == STATUS_STALE and not check:
                counts["rotated"] += 1
                return rotate_value(value, multi)
            if status != STATUS_CURRENT:
                locations.append(
                    {
                        "store": "plugin_configs",
                        "id": str(row.id),
                        "plugin": row.name,
                        "path": path,
                        "status": status,
                    }
                )
            return None

        updated_document, changed = walk_encrypted_strings(document, _visit)
        if changed and not check:
            await session.execute(
                _UPDATE_PLUGIN_CONFIG,
                {"config": json.dumps(updated_document), "id": row.id},
            )
    return {"counts": counts, "locations": locations}


async def sweep_plugin_tables(session) -> list[dict]:
    """Report ``plugin_*`` columns that hold token-shaped values (no writes).

    Identifiers come from ``information_schema`` and are rendered through
    ``table()``/``column()`` — never interpolated into SQL text (WR-05).
    """
    hits: list[dict] = []
    rows = (await session.execute(_PLUGIN_TABLE_COLUMNS)).all()
    for row in rows:
        probe = (
            select(literal(1))
            .select_from(table(row.table_name))
            .where(cast(column(row.column_name), Text).like(_CIPHERTEXT_PROBE))
            .limit(1)
        )
        if (await session.execute(probe)).first() is not None:
            hits.append({"table": row.table_name, "column": row.column_name})
    return hits


async def verify_primary_only(
    session, *, primary: Fernet, service_account_ids=None, plugin_ids=None
) -> dict:
    """Re-read every store in a fresh session and require the bare primary key.

    The ``MultiFernet`` is deliberately not used here: the verdict must prove
    that ``FERNET_KEY`` alone can read every stored value, which is exactly what
    removing ``FERNET_KEY_OLD`` will leave behind.
    """
    checked = 0
    failures: list[dict] = []

    scoped = service_account_ids is not None
    params = (
        {"ids": [str(row_id) for row_id in service_account_ids]} if scoped else {}
    )
    result = await session.execute(
        _service_account_select_sql(scoped=scoped, lock=False), params
    )
    for row in result.all():
        for column_name, _update_statement in _TOKEN_COLUMNS:
            stored = getattr(row, column_name)
            if not isinstance(stored, str) or not stored.startswith(_PREFIX):
                continue
            checked += 1
            try:
                primary.decrypt(stored[len(_PREFIX):].encode("utf-8"))
            except InvalidToken:
                failures.append(
                    {
                        "store": "service_accounts",
                        "id": str(row.id),
                        "column": column_name,
                    }
                )

    scoped = plugin_ids is not None
    params = {"ids": [str(row_id) for row_id in plugin_ids]} if scoped else {}
    result = await session.execute(_plugin_select_sql(scoped=scoped, lock=False), params)
    for row in result.all():
        document = json.loads(row.config)

        def _visit(path: str, value: str, row=row) -> None:
            nonlocal checked
            checked += 1
            try:
                primary.decrypt(value[len(_PREFIX):].encode("utf-8"))
            except InvalidToken:
                failures.append(
                    {
                        "store": "plugin_configs",
                        "id": str(row.id),
                        "plugin": row.name,
                        "path": path,
                    }
                )
            return None

        walk_encrypted_strings(document, _visit)

    return {"checked": checked, "failures": failures}


async def run_rotation(
    session_maker, *, check: bool, service_account_ids=None, plugin_ids=None
) -> dict:
    """Run the store passes in one transaction and verify in a new session.

    ``check`` never writes: it rolls the read transaction back. A scoped call
    (any id list given) only touches the stores whose ids were supplied — the
    unscoped pass never runs from a test. The plugin-table sweep runs only for
    an unscoped call, so it is reported as skipped otherwise.

    A failing stage rolls back and raises ``RotationError``; SQLAlchemy errors
    embed bound parameters, so the original message never reaches a log line.
    """
    primary, multi, old_key_configured = load_keys()
    stores: dict[str, dict] = {}
    locations: list[dict] = []
    scoped_run = service_account_ids is not None or plugin_ids is not None
    stage = "the session"

    try:
        async with session_maker() as session:
            if service_account_ids is None and scoped_run:
                service_accounts = {"counts": _skipped_counts(), "locations": []}
            else:
                stage = "the service-account pass"
                try:
                    service_accounts = await process_service_accounts(
                        session,
                        primary=primary,
                        multi=multi,
                        check=check,
                        ids=service_account_ids,
                    )
                except Exception:
                    await session.rollback()
                    raise RotationError(f"rotation failed in {stage}") from None

            if plugin_ids is None and scoped_run:
                plugin_configs = {"counts": _skipped_counts(), "locations": []}
            else:
                stage = "the plugin-config pass"
                try:
                    plugin_configs = await process_plugin_configs(
                        session,
                        primary=primary,
                        multi=multi,
                        check=check,
                        ids=plugin_ids,
                    )
                except Exception:
                    await session.rollback()
                    raise RotationError(f"rotation failed in {stage}") from None

            stage = "commit"
            try:
                if check:
                    await session.rollback()
                else:
                    await session.commit()
            except Exception:
                await session.rollback()
                raise RotationError(f"rotation failed in {stage}") from None
    except RotationError:
        raise
    except Exception:
        raise RotationError(f"rotation failed in {stage}") from None

    stores["service_accounts"] = service_accounts["counts"]
    stores["plugin_configs"] = plugin_configs["counts"]
    locations.extend(service_accounts["locations"])
    locations.extend(plugin_configs["locations"])

    stage = "verification"
    try:
        async with session_maker() as session:
            verified = await verify_primary_only(
                session,
                primary=primary,
                service_account_ids=service_account_ids,
                plugin_ids=plugin_ids,
            )
            await session.rollback()
    except Exception:
        raise RotationError(f"rotation failed in {stage}") from None

    sweep = {"skipped": True, "found": 0, "locations": []}
    if not scoped_run:
        stage = "the plugin-table sweep"
        try:
            async with session_maker() as session:
                hits = await sweep_plugin_tables(session)
                await session.rollback()
        except Exception:
            raise RotationError(f"rotation failed in {stage}") from None
        sweep = {"skipped": False, "found": len(hits), "locations": hits}
        for hit in hits:
            locations.append(
                {
                    "store": "plugin_tables",
                    "id": hit["table"],
                    "column": hit["column"],
                    "status": "foreign_ciphertext",
                }
            )
    stores["plugin_tables"] = sweep

    safe = not verified["failures"] and not sweep["found"]
    return {
        "ok": True,
        "mode": "check" if check else "rewrite",
        "old_key_configured": old_key_configured,
        "stores": stores,
        "locations": locations,
        "verified": verified,
        "exit_code": EXIT_SAFE if safe else EXIT_NOT_SAFE,
    }


def format_report(report: dict) -> str:
    """Render the operator report: per-store counts, locations and the verdict.

    Only counts and locations are printed — never a plaintext, a stored value or
    a token fragment, so the report can be logged verbatim.
    """
    lines: list[str] = []
    for name, counts in report["stores"].items():
        if counts.get("skipped"):
            lines.append(f"{name}: skipped (scoped run)")
            continue
        if "scanned" not in counts:
            found = counts.get("found", 0)
            lines.append(
                f"{name}: {found} column(s) hold ciphertext outside the managed stores"
            )
            continue
        lines.append(
            f"{name}: scanned={counts['scanned']} current={counts['current']} "
            f"stale={counts['stale']} rotated={counts['rotated']} "
            f"unreadable={counts['unreadable']} plaintext={counts['plaintext']}"
        )
    for location in report["locations"]:
        where = location.get("column") or location.get("path") or ""
        parts = [location["store"], location.get("id") or "", where, location["status"]]
        lines.append(" ".join(part for part in parts if part))

    checked = report["verified"]["checked"]
    if report["exit_code"] == EXIT_SAFE:
        if report["old_key_configured"]:
            lines.append(
                f"All {checked} encrypted values decrypt with FERNET_KEY alone — {SAFE_VERDICT}."
            )
        else:
            lines.append(
                f"No FERNET_KEY_OLD is configured; all {checked} encrypted values "
                "decrypt with FERNET_KEY."
            )
    else:
        reasons: list[str] = []
        if report["verified"]["failures"]:
            reasons.append(
                f"{len(report['verified']['failures'])} encrypted values cannot be "
                "read with FERNET_KEY alone"
            )
        if report["stores"].get("plugin_tables", {}).get("found"):
            reasons.append("foreign ciphertext exists in plugin-owned tables")
        lines.append(
            "NOT SAFE: " + "; ".join(reasons or ["the run is incomplete"])
            + " — keep FERNET_KEY_OLD configured."
        )
    return "\n".join(lines)
