"""Phase 287: the deploy-time plugin migration entry point (D-16).

Invoked once per deploy from ``backend/start.sh`` (Compose) and the Helm
migration Job as ``python -m app.plugin_system.migrate``. It ALWAYS exits 0 for
a plugin-attributable outcome — a single failing plugin must never abort the
deploy (D-16). Only an unexpected core-level exception (e.g. the database is
unreachable) or a fatal plugin-role misconfiguration returns 1, exactly like the
core ``alembic upgrade head`` command next to it stays fatal.

Phase 287.1: the deploy entry resolves the D-04/D-09 credential matrix here. When
both plugin credentials are absent it emits the single prominent D-10 warning and
runs the pre-287.1 privileged path; when they are configured it hands
:class:`PluginRoleCredentials` to the runner so every revision runs in the
hermetic role child (D-02/D-12). A partial/invalid configuration is fatal.
"""

import asyncio
import logging
import sys

from app.logging import configure_logging
from app.plugin_system.bootstrap_role import (
    PLUGIN_WEAKENED_WARNING,
    PluginRoleConfigError,
    resolve_plugin_role_config,
)
from app.plugin_system.migrations import (
    STATUS_FAILED,
    STATUS_UP_TO_DATE,
    MigrationRunResult,
    PluginRoleCredentials,
    run_plugin_migrations,
)

logger = logging.getLogger(__name__)


def resolve_plugin_role() -> PluginRoleCredentials | None:
    """Resolve the credential matrix; warn once when the boundary is weakened.

    Returns ``None`` (the additive fallback path) only when BOTH credentials are
    absent, emitting the D-10 warning exactly once. A set ``PLUGIN_DB_USER``
    without a password, or an invalid identifier, raises
    :class:`PluginRoleConfigError` so the deploy aborts before any revision runs.
    """
    config = resolve_plugin_role_config()
    if not config.configured:
        logger.warning(PLUGIN_WEAKENED_WARNING)
        return None
    return PluginRoleCredentials(config.role_name, config.password)


async def run_deploy_migrations() -> MigrationRunResult:
    """Awaitable deploy core: resolve the role, then run the plugin migrations."""
    return await run_plugin_migrations(plugin_role=resolve_plugin_role())


def _log_result(result: MigrationRunResult) -> None:
    if result.locked_out:
        logger.warning(
            "Plugin migration run skipped: another runner holds the migration lock."
        )
    for outcome in result.outcomes:
        if outcome.status == STATUS_FAILED:
            logger.warning(
                "Plugin %r migration did not complete (reason: %s)",
                outcome.plugin_name,
                outcome.reason,
            )
        else:
            log = logger.debug if outcome.status == STATUS_UP_TO_DATE else logger.info
            log(
                "Plugin %r migration %s (revision %s)",
                outcome.plugin_name,
                outcome.status,
                outcome.revision,
            )


def main() -> int:
    configure_logging("plugin-runner")
    try:
        result = asyncio.run(run_deploy_migrations())
    except PluginRoleConfigError as exc:
        logger.error("Plugin database role configuration is invalid: %s", exc)
        return 1
    except Exception:
        logger.exception("Plugin migration runner failed at the core level")
        return 1

    _log_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
