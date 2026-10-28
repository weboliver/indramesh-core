"""Config API routes.

Two LLM routes, both gated by the existing ``config:read`` permission (D-05):

- ``GET /config/llm`` is provider-aware and never exposes the API key (D-08).
  It reads the non-secret introspection view off the ``app/llm`` facade
  (``get_llm_client()`` never raises), so an invalid env yields a 200 with
  ``status="misconfigured"`` instead of a 500.
- ``POST /config/llm/test`` runs one classified connection probe over the real
  ``app/llm`` structured path (D-06/D-07). POST because it triggers an active
  outbound call; the response is a fixed category + sanitized message only.
"""

from fastapi import APIRouter, Depends

from app.llm import get_llm_client
from app.llm.probe import run_llm_connection_test
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission


router = APIRouter(prefix="/config", tags=["config"])


def _probe_transport():
    """The single probe-injection point (returns ``None`` in production).

    Tests monkeypatch this to return an ``httpx.MockTransport`` so the probe
    runs entirely in-process and never reaches a real provider.
    """
    return None


@router.get("/llm")
async def get_llm_config(
    current_user: UserResponse = Depends(require_permission("config", "read")),
):
    """Provider-aware, key-free LLM status (D-08)."""
    client = get_llm_client()
    return {
        "provider": client.provider,
        "model": client.model,
        "base_url": client.base_url,
        "mode": client.mode,
        "configured": client.is_configured(),
        "api_key_set": client.api_key_set,
        "status": client.status,
        "config_error": client.config_error,
    }


@router.post("/llm/test")
async def test_llm_connection(
    current_user: UserResponse = Depends(require_permission("config", "read")),
):
    """Run the classified LLM connection probe (D-05/D-06/D-07)."""
    return await run_llm_connection_test(transport=_probe_transport())
