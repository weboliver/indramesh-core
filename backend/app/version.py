"""Single writable source of the IndraMesh application version.

``__version__`` is the one writable version. A release writes it and the
locations that derive from it; everything else reads or checks the result:

* ``GET /health`` and GraphQL ``health.version`` read it directly, and
  ``FastAPI(version=...)`` publishes it in the OpenAPI metadata.
* ``release.sh <version>`` writes this file and, from the same input,
  ``frontend/package.json``, ``deploy/helm/indramesh/Chart.yaml`` (``version``
  and ``appVersion``) and ``plugins/example/plugin.toml``. ``plugins/example``
  ships inside the core, so its manifest follows the core version.
* ``scripts/publish.sh``'s ``guard_versions()`` checks the core target's derived
  locations against this value. A plugin target is checked differently: plugin
  versions are decoupled from the core version, so it verifies a three-part
  SemVer plus ``is_compatible(CORE_API_VERSION, api_version)``.

This module is deliberately in the backend build context (``context: ./backend``)
so it ships with the image without a Dockerfile or Compose change, and it is
distinct from ``app.plugin_system.versioning.CORE_API_VERSION``, which is the
plugin API contract version and is never changed by a release.
"""

__version__ = "1.0.1"
