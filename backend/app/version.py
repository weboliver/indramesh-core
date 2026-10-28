"""Single writable source of the IndraMesh application version.

``__version__`` is the ONE place a release changes. Everything else derives from
it at runtime or is checked against it:

* ``GET /health`` and GraphQL ``health.version`` read it directly, and
  ``FastAPI(version=...)`` publishes it in the OpenAPI metadata.
* ``scripts/publish.sh``'s ``guard_versions()`` compares every derived location
  (``frontend/package.json``, ``deploy/helm/indramesh/Chart.yaml`` and each
  ``plugins/*/plugin.toml``) against this value and aborts the publish run on
  any mismatch.
* The future ``scripts/release.sh`` only has to edit this file; the derived
  locations are checked, never written, from here on.

This module is deliberately in the backend build context (``context: ./backend``)
so it ships with the image without a Dockerfile or Compose change, and it is
distinct from ``app.plugin_system.versioning.CORE_API_VERSION``, which is the
plugin API contract version and must not be changed by a release.
"""

__version__ = "1.0.0"
