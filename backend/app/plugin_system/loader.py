"""Plugin discovery — entry points + directory scan (D-03 / PLUGCORE-04/05).

``DiscoveryResult`` is the shared return shape for every discovery source: the
entry-point path and the directory-scan path both return it, so ``registry.py``'s
success/error handling never changes per discovery source.

The loader isolates every per-plugin failure into an error ``DiscoveryResult``
— one broken plugin must never abort the scan of the others (PLUGOPS-01).

``discover_all()`` composes both sources: entry-point discovery first, directory
scan second (RESEARCH.md Open Question 2). A directory-scan plugin whose name
collides with an already-discovered entry-point plugin is rejected as an error
result rather than silently shadowing the installed package.
"""

import importlib.metadata
import importlib.resources
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from app.logging.categories import register_plugin_module
from app.plugin_system.base import IndraMeshPlugin
from app.plugin_system.manifest import PluginManifest, load_manifest_toml
from app.plugin_system.versioning import CORE_API_VERSION, is_compatible

logger = logging.getLogger(__name__)

# D-03: the published entry-point group every plugin package declares.
ENTRY_POINT_GROUP = "indramesh.plugins"

# T-282-04: plugin directory names are restricted to a safe character set so a
# crafted name can never contribute path separators or traversal segments.
_PLUGIN_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# A plugin update stages the new tree as ``<name>.new`` and moves the old one to
# ``<name>.old`` on the same volume. Neither is a plugin, also after an aborted
# update that left one behind.
_UPDATE_STAGING_SUFFIXES = (".new", ".old")

# CORESEAM-06: every directory plugin is imported under this core-controlled,
# deterministic prefix — never a plugin-supplied name (T-289-06-01). A package
# directory builds on it: the manifest's ``module`` field is imported as the
# submodule ``indramesh_plugin_<dir>.<module>``.
_PLUGIN_MODULE_PREFIX = "indramesh_plugin_"

# T-289-06-03: set on a package entry THIS loader fully executed. Only a marked
# entry whose file still points at the requested directory is reused as the
# canonical identity — an unknown or partially executed entry is never trusted.
_PACKAGE_LOADED_ATTR = "__indramesh_plugin_loaded__"


@dataclass
class DiscoveryResult:
    """Outcome of discovering one plugin from any discovery source.

    ``error is None`` means success. On success ``name`` is the plugin's
    manifest-declared identity; on an early failure (invalid name, unreadable or
    malformed manifest) it falls back to the directory entry name so the
    registry still has a stable key to record the error state under.
    """

    name: str
    manifest: PluginManifest | None = None
    plugin_cls: type | None = None
    error: str | None = None
    # NYQ-282-01: an explicit, loader-declared duplicate rejection — a later
    # otherwise-successful candidate whose manifest name was already claimed by
    # an earlier discovery source. ``load_results()`` must NOT route this through
    # ``_mark_error()`` (that would downgrade the accepted winner); it is never
    # inferred from the error text.
    duplicate_rejected: bool = False
    # Phase 287 (PLUGDB-03): the resolved on-disk plugin directory for a
    # directory-scan plugin, so ``app.plugin_system.migrations`` can join the
    # manifest's ``migrations_dir`` onto it. Additive with a ``None`` default —
    # entry-point plugins have no directory-scan base and stay ``None`` (their
    # ``importlib.resources`` resolution is plan 287-02's contract, RESEARCH
    # Pitfall 3).
    base_dir: str | None = None


def validate_plugin_name(name: str) -> DiscoveryResult | None:
    """Return an error result if ``name`` is not a safe plugin directory name.

    Runs BEFORE any path is built (T-282-04).
    """
    if not _PLUGIN_NAME_RE.match(name):
        return DiscoveryResult(
            name=name,
            error=(
                f"Invalid plugin directory name {name!r}: must match "
                f"{_PLUGIN_NAME_RE.pattern} (path-traversal guard, T-282-04)"
            ),
        )
    return None


def discover_directory_plugins(plugins_dir: str) -> list[DiscoveryResult]:
    """Scan ``plugins_dir`` for ``<name>/plugin.toml`` plugin directories.

    A missing ``plugins_dir`` yields zero results (not an error) — a fresh
    install simply has no plugins yet.
    """
    if not os.path.isdir(plugins_dir):
        return []

    base = Path(plugins_dir).resolve()
    results: list[DiscoveryResult] = []

    for entry in sorted(os.listdir(plugins_dir)):
        if entry.endswith(_UPDATE_STAGING_SUFFIXES):
            continue

        invalid = validate_plugin_name(entry)
        if invalid is not None:
            results.append(invalid)
            continue

        if not os.path.isdir(os.path.join(plugins_dir, entry)):
            continue

        try:
            results.append(_discover_one(entry, base))
        except Exception as exc:
            logger.exception("Plugin %r failed to load", entry)
            results.append(
                DiscoveryResult(name=entry, error=f"{type(exc).__name__}: {exc}")
            )

    return results


def discover_all(plugins_dir: str) -> list[DiscoveryResult]:
    """Discover plugins from every source: entry points first, directory scan second.

    Entry-point discovery runs FIRST so a locally-dropped directory-scan plugin
    can never silently shadow an installed package (RESEARCH.md Open Question 2).
    A result whose manifest name collides with an ALREADY-CLAIMED name — from
    either source, including two entry points or two directory-scan plugins
    sharing the same manifest ``name`` (WR-08) — is converted into an error
    ``DiscoveryResult`` marked ``duplicate_rejected=True`` — loud, never a silent
    override and never a silent drop. The marker lets ``registry.py`` keep the
    accepted winner untouched (NYQ-282-01) while ordinary discovery failures
    still follow its ``PluginState.ERROR`` isolation path.
    """
    claimed: dict[str, str] = {}

    def _claim(result: DiscoveryResult, source_label: str) -> DiscoveryResult:
        if result.error is not None:
            return result

        prior_source = claimed.get(result.name)
        if prior_source is not None:
            logger.error(
                "Plugin %r already discovered via %s — %s duplicate rejected",
                result.name, prior_source, source_label,
            )
            return DiscoveryResult(
                name=result.name,
                manifest=result.manifest,
                error=(
                    f"plugin {result.name!r} already discovered via "
                    f"{prior_source} — {source_label} duplicate rejected"
                ),
                duplicate_rejected=True,
            )

        claimed[result.name] = source_label
        return result

    results = [
        _claim(result, "entry-point") for result in discover_entry_point_plugins()
    ]
    results += [
        _claim(result, "directory-scan")
        for result in discover_directory_plugins(plugins_dir)
    ]

    return results


def discover_entry_point_plugins() -> list[DiscoveryResult]:
    """Discover every plugin declared under the ``indramesh.plugins`` group.

    Entry points address the plugin class as ``paket.modul:PluginKlasse``
    (D-03); ``ep.load()`` imports the module AND resolves the attribute. The
    plugin's own ``plugin.toml`` is then read from its importable package via
    ``importlib.resources`` — deliberately NOT from ``ep.dist``, which is not
    populated for a locally-importable, non-pip-installed package.

    ``ep.load()`` and everything that follows it sit in ONE try/except: a
    module-import failure and a manifest-read failure are treated identically
    as that one plugin's error result (RESEARCH.md Pitfall 2 — import side
    effects already happened once ``ep.load()`` ran), so one broken entry point
    never prevents a sibling entry point in the same pass from succeeding.
    """
    results: list[DiscoveryResult] = []

    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            plugin_cls = ep.load()
            manifest = _load_entry_point_manifest(plugin_cls)
            _check_api_version(manifest)
        except Exception as exc:
            logger.exception("Entry-point plugin %r failed to load", ep.name)
            results.append(
                DiscoveryResult(name=ep.name, error=f"{type(exc).__name__}: {exc}")
            )
            continue

        results.append(
            DiscoveryResult(
                name=manifest.name,
                manifest=manifest,
                plugin_cls=plugin_cls,
                base_dir=_entry_point_base_dir(plugin_cls),
            )
        )

    return results


def _package_name_for(plugin_cls: type) -> str | None:
    """Resolve the importable package a plugin class lives in.

    Shared by entry-point manifest reading and Phase 287's migrations-directory
    resolver: the package is derived from the loaded class's module
    (``__package__``, falling back to the module's parent) — never from the entry
    point's distribution, which a test/monkeypatched or locally-importable
    package does not have.
    """
    module_name = plugin_cls.__module__
    module = sys.modules.get(module_name)
    package_name = getattr(module, "__package__", None) or module_name.rpartition(".")[0]
    return package_name or None


def _entry_point_base_dir(plugin_cls: type) -> str | None:
    """Best-effort on-disk base directory for an entry-point plugin (Phase 287).

    Returns the plugin package's real directory when ``importlib.resources.files``
    resolves to one, else ``None`` — a packaged (zip/wheel) plugin has no real
    directory, so ``resolve_migrations_dir`` falls back to resolving its
    migrations directory through ``importlib.resources`` instead.
    """
    try:
        package_name = _package_name_for(plugin_cls)
        if not package_name:
            return None
        root = Path(str(importlib.resources.files(package_name)))
        return str(root) if root.is_dir() else None
    except Exception:
        logger.exception(
            "Could not resolve an on-disk base dir for entry-point class %r",
            plugin_cls,
        )
        return None


def _load_entry_point_manifest(plugin_cls: type) -> PluginManifest:
    """Read an entry-point plugin's own ``plugin.toml`` from its package data."""
    package_name = _package_name_for(plugin_cls)
    if not package_name:
        raise ValueError(
            f"entry-point class {plugin_cls.__qualname__} lives in top-level module "
            f"{plugin_cls.__module__!r} — no package to read plugin.toml from"
        )

    manifest_path = importlib.resources.files(package_name).joinpath("plugin.toml")
    return load_manifest_toml(manifest_path.read_bytes())


def resolve_migrations_dir(result: DiscoveryResult):
    """Resolve a plugin's declared migrations directory (PLUGDB-01/T-287-09).

    Returns ``None`` when the plugin declares no ``[database]`` section. For a
    result with a real ``base_dir`` (directory-scan discovery, or an entry point
    whose package is a real directory) returns a resolved ``pathlib.Path`` inside
    the plugin directory, applying the loader's own path-traversal guard. For a
    result with no ``base_dir`` it derives the plugin's package from
    ``result.plugin_cls.__module__`` and returns an ``importlib.resources``
    traversable that the caller must materialise with
    ``importlib.resources.as_file(...)`` for the duration of the migration.

    Raises ``ValueError`` for a ``..``-bearing value that escapes the plugin
    directory, and when no package can be derived for a result with no base dir
    (never guesses a package).
    """
    manifest = result.manifest
    if manifest is None or manifest.database is None:
        return None
    migrations_dir = manifest.database.migrations_dir

    if result.base_dir is not None:
        base = Path(result.base_dir).resolve()
        candidate = (base / migrations_dir).resolve()
        if not candidate.is_relative_to(base):
            raise ValueError(
                f"migrations directory {candidate} escapes the plugin directory "
                f"{base} (path-traversal guard, T-287-09)"
            )
        return candidate

    plugin_cls = result.plugin_cls
    package_name = _package_name_for(plugin_cls) if plugin_cls is not None else None
    if not package_name:
        raise ValueError(
            f"plugin {result.name!r} declares migrations_dir but no package could "
            "be derived to resolve it"
        )
    return importlib.resources.files(package_name).joinpath(migrations_dir)


def _check_api_version(manifest: PluginManifest) -> None:
    """Raise ``ValueError`` when ``manifest.api_version`` is not compatible.

    PLUGCORE-08 / T-282-08: an incompatible plugin must be rejected before its
    code runs. For directory-scan discovery this runs BEFORE the module import
    (so the plugin's Python is never executed); for entry-point discovery the
    class must already be resolved to locate the package's ``plugin.toml``, so
    the gate runs before instantiation/``on_load`` instead.

    A malformed ``api_version`` raises ``parse_semver``'s ``ValueError``
    unchanged; the caller isolates either failure into an error
    ``DiscoveryResult`` exactly like any other discovery failure.
    """
    compatible = is_compatible(CORE_API_VERSION, manifest.api_version)
    if not compatible:
        raise ValueError(
            f"plugin {manifest.name!r} targets api_version "
            f"{manifest.api_version}, incompatible with core {CORE_API_VERSION}"
        )


def _discover_one(dir_name: str, base: Path) -> DiscoveryResult:
    """Load a single plugin directory. Raises on any failure — the caller isolates."""
    plugin_dir = base / dir_name
    manifest = load_manifest_toml((plugin_dir / "plugin.toml").read_bytes())

    # PLUGCORE-08: rejected BEFORE the module import — an incompatible plugin's
    # Python code must never execute.
    _check_api_version(manifest)

    # D-16 / research Open Question 4: map the core-controlled module prefix
    # (indramesh_plugin_<dir>) to the manifest name before the plugin is
    # imported, so its loggers land in plugin.<manifest name> even when the
    # directory name differs.
    register_plugin_module(dir_name, manifest.name)

    if not manifest.module:
        raise ValueError(
            "manifest is missing the 'module' field required for directory-scan discovery"
        )

    package_entry = plugin_dir / "__init__.py"
    if package_entry.is_file():
        # CORESEAM-06: a directory carrying ``__init__.py`` is a real Python
        # package — the manifest's ``module`` field names the entry SUBMODULE,
        # so relative imports resolve under one canonical identity.
        package_name = f"{_PLUGIN_MODULE_PREFIX}{dir_name}"
        module = _import_package_from_dir(
            package_name, plugin_dir, package_entry, manifest.module, base
        )
        spec_name = f"{package_name}.{manifest.module}"
    else:
        module_path = (plugin_dir / f"{manifest.module}.py").resolve()
        if not module_path.is_relative_to(base):
            raise ValueError(
                f"module path {module_path} escapes the plugins directory "
                f"(path-traversal guard, T-282-04)"
            )

        module = _import_module_from_path(
            f"{_PLUGIN_MODULE_PREFIX}{dir_name}", module_path
        )
        spec_name = f"{_PLUGIN_MODULE_PREFIX}{dir_name}"

    plugin_cls = _resolve_plugin_class(module, spec_name)

    return DiscoveryResult(
        name=manifest.name,
        manifest=manifest,
        plugin_cls=plugin_cls,
        base_dir=str(plugin_dir),
    )


def import_module_from_path(spec_name: str, module_path: Path):
    """Public alias of :func:`_import_module_from_path` for plugin authors.

    Phase 287: a plugin's own ``module.py`` loads its ``database.py`` metadata
    module through this helper (``plugins/**`` is not on ``sys.path``, so a bare
    ``import database`` must not be used) without duplicating the importlib
    boilerplate in every plugin.
    """
    return _import_module_from_path(spec_name, Path(module_path))


def _import_module_from_path(spec_name: str, module_path: Path):
    """Import a module from an explicit filesystem path.

    Never a bare ``import_module(module)`` — that risks silently resolving to an
    unrelated same-named module already on ``sys.path``. The module is
    registered in ``sys.modules`` BEFORE ``exec_module`` so dataclasses/enums
    defined inside it resolve by name.
    """
    spec = importlib.util.spec_from_file_location(spec_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build an import spec for {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec_name, None)
        raise
    return module


def _import_package_from_dir(
    package_name: str,
    package_dir: Path,
    package_entry: Path,
    entry_module: str,
    base: Path,
):
    """Import a directory plugin as a Python package (CORESEAM-06).

    The package root is ``package_name`` (core-controlled, deterministic) built
    from ``package_dir/__init__.py`` with the directory as its search location;
    the manifest's ``entry_module`` is then imported as
    ``<package_name>.<entry_module>``, so the plugin's own relative imports
    (``from .database import Base``) resolve through that one identity instead
    of a second, differently named module.

    Both the package entry and the entry module are containment-checked with
    their SYMLINKS RESOLVED (T-289-06-01), so neither a crafted name nor a
    symlink can point the importer outside the plugins directory.

    A package entry this loader already executed completely is reused as the
    canonical identity (T-289-06-03). A failed attempt removes only the entries
    IT inserted — a pre-existing ``sys.modules`` entry is restored, never
    removed, so a broken plugin can never shadow another module.
    """
    init_path = package_entry.resolve()
    if not init_path.is_relative_to(base):
        raise ValueError(
            f"package entry path {init_path} escapes the plugins directory "
            f"(path-traversal guard, T-289-06-01)"
        )

    entry_path = (package_dir / f"{entry_module}.py").resolve()
    if not entry_path.is_relative_to(base):
        raise ValueError(
            f"module path {entry_path} escapes the plugins directory "
            f"(path-traversal guard, T-289-06-01)"
        )

    loaded_entry = _loaded_package_entry(package_name, package_dir, entry_module)
    if loaded_entry is not None:
        return loaded_entry

    spec = importlib.util.spec_from_file_location(
        package_name, init_path, submodule_search_locations=[str(package_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build a package import spec for {init_path}")

    # Snapshot BEFORE executing: the plugin's own ``__init__`` may import
    # submodules, so cleanup must undo exactly this attempt's work.
    snapshot = _plugin_module_entries(package_name)
    # A previous identity for a DIFFERENT directory must not lend its stale
    # submodules to this import (module shadowing, T-289-06-03). The snapshot
    # above restores it verbatim if this attempt fails.
    _clear_plugin_module_entries(package_name)

    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    try:
        spec.loader.exec_module(module)
        setattr(module, _PACKAGE_LOADED_ATTR, True)
        return importlib.import_module(f"{package_name}.{entry_module}")
    except Exception:
        _restore_plugin_module_entries(package_name, snapshot)
        raise


def _loaded_package_entry(package_name: str, package_dir: Path, entry_module: str):
    """Return the entry submodule of a COMPLETE, matching package identity.

    ``None`` means "no reusable identity": the name is absent, was not loaded by
    this loader (marker missing), or points at a different directory (two
    plugins with the same directory name). Only then does the caller re-import.
    """
    module = sys.modules.get(package_name)
    if module is None or not getattr(module, _PACKAGE_LOADED_ATTR, False):
        return None

    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    try:
        if Path(module_file).resolve().parent != package_dir.resolve():
            return None
    except OSError:
        return None

    entry = sys.modules.get(f"{package_name}.{entry_module}")
    if entry is None:
        entry = importlib.import_module(f"{package_name}.{entry_module}")
    return entry


def _plugin_module_entries(package_name: str) -> dict[str, object]:
    """Snapshot every ``sys.modules`` entry in one package's namespace."""
    prefix = f"{package_name}."
    return {
        name: module
        for name, module in sys.modules.items()
        if name == package_name or name.startswith(prefix)
    }


def _clear_plugin_module_entries(package_name: str) -> None:
    """Drop every ``sys.modules`` entry in one package's namespace."""
    prefix = f"{package_name}."
    for name in [
        name
        for name in sys.modules
        if name == package_name or name.startswith(prefix)
    ]:
        del sys.modules[name]


def _restore_plugin_module_entries(
    package_name: str, snapshot: dict[str, object]
) -> None:
    """Undo a package attempt: restore pre-existing entries, drop the new ones."""
    prefix = f"{package_name}."
    for name in [
        name
        for name in sys.modules
        if name == package_name or name.startswith(prefix)
    ]:
        if name in snapshot:
            sys.modules[name] = snapshot[name]
        else:
            del sys.modules[name]


def _resolve_plugin_class(module, spec_name: str) -> type:
    """Find the single concrete ``IndraMeshPlugin`` subclass defined in ``module``.

    Excludes capability interfaces (``_capability_name`` in the class's own
    ``__dict__``) and classes merely imported into the module.
    """
    candidates: list[type] = []
    for obj in vars(module).values():
        if not isinstance(obj, type) or obj in candidates:
            continue
        if obj.__module__ != spec_name:
            continue
        if not issubclass(obj, IndraMeshPlugin):
            continue
        if "_capability_name" in obj.__dict__:
            continue
        candidates.append(obj)

    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one IndraMeshPlugin subclass in module "
            f"{spec_name!r}, found {len(candidates)}"
        )
    return candidates[0]
