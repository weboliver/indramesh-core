"""Strict resolver for the non-secret runtime mode signal (Phase 255, D-12).

``INDRAMESH_RUNTIME_MODE`` is the single explicit, non-secret signal shared by
startup (``init_service``) and the System Logs provider selection:

- ``compose`` selects the Docker Compose path (developer initial-password
  artifact, Docker provider).
- ``kubernetes`` selects the Kubernetes path (Secret-provided ``ADMIN_PASSWORD``,
  in-cluster pod-log provider, no developer password artifact, read-only root).

Missing or unknown values fail closed with ``RuntimeError`` so a deployment can
never silently fall back to the wrong provider or artifact behavior (D-12).
Docker Compose sets the variable to ``compose`` in docker-compose.yml; the
shared Helm helper ``indramesh.kubernetesEnv`` emits ``kubernetes`` for every
later backend/worker workload.
"""

import os

_RUNTIME_MODE_ENV = "INDRAMESH_RUNTIME_MODE"

_MODE_COMPOSE = "compose"
_MODE_KUBERNETES = "kubernetes"

_VALID_MODES = frozenset({_MODE_COMPOSE, _MODE_KUBERNETES})


def resolve_runtime_mode() -> str:
    """Return the resolved runtime mode: ``compose`` or ``kubernetes``.

    Raises RuntimeError for a missing or unknown ``INDRAMESH_RUNTIME_MODE`` — the
    resolver never guesses a mode, so a misconfigured deployment fails loudly
    instead of silently choosing the wrong provider or artifact behavior.
    """
    mode = os.environ.get(_RUNTIME_MODE_ENV, "")
    if mode not in _VALID_MODES:
        raise RuntimeError(
            f"{_RUNTIME_MODE_ENV} must be '{_MODE_COMPOSE}' or "
            f"'{_MODE_KUBERNETES}' (got {mode!r}) — missing/unknown modes fail "
            "closed"
        )
    return mode


def is_kubernetes_mode() -> bool:
    """True when the runtime is Kubernetes (in-cluster, restricted workload)."""
    return resolve_runtime_mode() == _MODE_KUBERNETES