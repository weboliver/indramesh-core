"""Classified LLM connection probe (LLM-03).

``run_llm_connection_test`` exercises the **real** ``app/llm`` structured path
(exactly one minimal completion) and classifies the outcome into a single
sanitized category — never a provider body, never the key, never the prompt.

Mirrors the v20 Jira ``run_connection_test`` shape (``plugins/jira/connections.py``):
a fixed per-category message map plus an injectable ``httpx`` transport. Tests
inject an ``httpx.MockTransport`` through the config route's ``_probe_transport``
seam, so no test ever reaches a real provider.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from app.llm.client import EGRESS_HINT, classify_exception, get_llm_client
from app.llm.errors import LLMError

# The probe's response model: a single trivial field. The structured call only
# has to succeed end-to-end (auth + model + mode + instructor); the value is
# never surfaced.
class _ProbeOut(BaseModel):
    status: str


# The full D-07 outcome vocabulary plus the reviewed ``not_configured``
# short-circuit. Messages are fixed and carry no provider body or secret.
_PROBE_MESSAGES: dict[str, str] = {
    "ok": "The LLM connection is healthy.",
    "auth_failed": "The LLM provider rejected the API key.",
    "unreachable": "The LLM provider could not be reached." + EGRESS_HINT,
    "timeout": "The LLM request timed out." + EGRESS_HINT,
    "model_not_found": "The configured LLM model was not found on the provider.",
    "rate_limited": "The LLM provider rate-limited the request.",
    "invalid_response": "The LLM did not return a valid structured response.",
    "not_configured": "LLM is not configured. Set LLM_API_KEY and LLM_MODEL.",
}

# A fixed, data-free probe prompt (T-302-09): no schema/object content leaks.
_PROBE_PROMPT_MESSAGES: list[dict] = [
    {
        "role": "user",
        "content": "Connection probe. Reply with the single word: ok.",
    }
]


def _probe_result(category: str, message: Optional[str] = None) -> dict:
    """The one sanitized result shape every outcome funnels through."""
    if message is None:
        message = _PROBE_MESSAGES[category]
    return {"ok": category == "ok", "category": category, "message": message}


async def run_llm_connection_test(*, transport: Any = None) -> dict:
    """Run one minimal structured completion and classify the outcome (D-06/D-07).

    ``transport`` is the single test-injection point: when provided, the facade
    returns a dedicated transport-bound client that bypasses the process
    singleton, so the probe never mutates the configured client and a mock
    transport can never reach a real provider.

    ``max_retries=0`` is deliberate: a connection test must issue exactly one
    provider request, never a re-ask loop (D-06, review correction 2).
    """
    client = get_llm_client(transport=transport)

    if not client.is_configured():
        # No key or an invalid env means no active call is meaningful.
        # ``auth_failed`` would mislead an operator whose key is simply unset
        # (D-07 amended by review): the sanitized config error (invalid env) or
        # the fixed not-configured message (no key) is the honest answer.
        return _probe_result("not_configured", client.config_error)

    try:
        await client.generate_structured(
            _PROBE_PROMPT_MESSAGES, _ProbeOut, max_retries=0
        )
    except LLMError as exc:
        category = classify_exception(exc)
        if category not in _PROBE_MESSAGES:
            category = "invalid_response"
        return _probe_result(category)

    return _probe_result("ok")
