"""Logger-name -> category mapping (D-16, longest prefix wins)."""

import re

# CORESEAM-06: plugin_system/loader.py:_PLUGIN_MODULE_PREFIX. The literal is
# duplicated on purpose instead of imported: plan 298-03 makes loader.py import
# this module (register_plugin_module), so importing the loader here would form
# an import cycle once that lands.
_PLUGIN_MODULE_PREFIX = "indramesh_plugin_"

# Directory name -> manifest name, filled by the loader at discovery time so a
# plugin whose directory differs from its manifest name still logs under
# plugin.<manifest name> (research Open Question 4).
_PLUGIN_DIR_NAMES: dict[str, str] = {}

_CONNECTOR_PREFIX = "app.services.connectors."
_CONNECTOR_SUFFIX = "_connector"

PREFIX_MAP = (
    ("sqlalchemy", "db"),
    ("asyncpg", "db"),
    ("alembic", "db"),
    ("app.middleware.query_profiler", "db"),
    ("arq", "task"),
    ("apscheduler", "task"),
    ("task", "task"),
    ("uvicorn", "app"),
    ("uvicorn.error", "app"),
    ("uvicorn.access", "access"),
    ("access", "access"),
    ("security", "security"),
    ("app.llm", "llm"),
    ("openai", "llm"),
    ("instructor", "llm"),
)

_SORTED_PREFIXES = tuple(
    sorted(PREFIX_MAP, key=lambda item: len(item[0]), reverse=True)
)

CATEGORY_RE = re.compile(
    r"^(app|access|security|task|db|llm|connector\.[a-z0-9_-]+|plugin\.[a-z0-9_-]+)$"
)


def register_plugin_module(dir_name: str, plugin_name: str) -> None:
    """Map a plugin directory name to its manifest name (D-16)."""
    _PLUGIN_DIR_NAMES[dir_name.lower()] = plugin_name.lower()


def is_valid_category(value: str) -> bool:
    # ``fullmatch`` (not ``match``) so a trailing newline is rejected: Python's
    # ``$`` also matches immediately before a final "\n", which would accept
    # an unusable category like "app\n" while ``normalize_category`` and
    # ``parse_report`` already use ``fullmatch`` (WR-02).
    return CATEGORY_RE.fullmatch(value) is not None


def category_for(logger_name: str) -> str:
    if logger_name.startswith("plugin."):
        name = logger_name[len("plugin."):].split(".", 1)[0]
        return f"plugin.{name.lower()}" if name else "app"

    if logger_name.startswith(_PLUGIN_MODULE_PREFIX):
        directory = logger_name[len(_PLUGIN_MODULE_PREFIX):].split(".", 1)[0]
        if not directory:
            return "app"
        name = _PLUGIN_DIR_NAMES.get(directory.lower(), directory)
        return f"plugin.{name.lower()}"

    if logger_name.startswith(_CONNECTOR_PREFIX):
        module = logger_name[len(_CONNECTOR_PREFIX):].split(".", 1)[0]
        if module.endswith(_CONNECTOR_SUFFIX):
            module = module[: -len(_CONNECTOR_SUFFIX)]
        return f"connector.{module.lower()}" if module else "app"

    for prefix, category in _SORTED_PREFIXES:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return category

    return "app"
