"""Provider-neutral LLM client construction and the ``LLMClient`` facade.

One OpenAI-compatible Chat Completions wire protocol (D-14): a raw
``AsyncOpenAI`` transport wrapped by ``instructor.from_openai`` for structured
Pydantic output. The vendor imports are lazy (inside ``_build_client``) so
importing ``app.llm`` never pulls a provider SDK (D-15).

The process-wide singleton is the single monkeypatch seam the service tests
use. An injected ``httpx`` transport gets a dedicated, transport-bound client
that bypasses the singleton entirely — so a test/mock transport can never be
served to a normal call and no test can reach a real provider.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from app.llm.errors import (
    LLMAuthFailed,
    LLMConfigError,
    LLMError,
    LLMModelNotFound,
    LLMNotConfigured,
    LLMRateLimited,
    LLMResponseInvalid,
    LLMTimeout,
    LLMUnreachable,
)
from app.llm.settings import (
    LLMSettings,
    ResolvedLLMConfig,
    resolve_llm_config,
)

_logger = logging.getLogger("app.llm")

# Explicit request timeout (D-01 keeps the env contract at four + LLM_MODE):
# a module constant, not a new env var.
REQUEST_TIMEOUT_SECONDS = 60.0

# mode string -> instructor.Mode attribute name (mapped in client.py, not
# settings.py, so settings.py stays vendor-free).
_MODE_ATTRIBUTES = {
    "tools": "TOOLS",
    "json_schema": "JSON_SCHEMA",
    "json": "JSON",
    "md_json": "MD_JSON",
}

_ERROR_BY_CATEGORY: dict[str, type[LLMError]] = {
    "auth_failed": LLMAuthFailed,
    "unreachable": LLMUnreachable,
    "timeout": LLMTimeout,
    "model_not_found": LLMModelNotFound,
    "rate_limited": LLMRateLimited,
    "invalid_response": LLMResponseInvalid,
}

# Phase 318 (K8S-LLM-EGRESS-01): inside a Kubernetes pod a dropped provider
# connection is almost always the NetworkPolicy egress, not the provider.
EGRESS_HINT = (
    " On Kubernetes, check that the NetworkPolicy egress admits the provider"
    " (docs/CONFIGURATION.md)."
    if os.environ.get("INDRAMESH_RUNTIME_MODE") == "kubernetes"
    else ""
)

_ERROR_MESSAGES = {
    "auth_failed": "LLM authentication failed. Check the API key.",
    "unreachable": "The LLM provider could not be reached." + EGRESS_HINT,
    "timeout": "The LLM request timed out." + EGRESS_HINT,
    "model_not_found": "The configured LLM model was not found on the provider.",
    "rate_limited": "The LLM provider rate-limited the request.",
    "invalid_response": "The LLM did not return a valid structured response.",
}


def classify_exception(exc: BaseException) -> str:
    """Map a provider/instructor failure to a categorized outcome (D-07).

    instructor's ``retry_async`` wraps *every* failure — including provider
    ``AuthenticationError``/``APITimeoutError``/``APIConnectionError`` — in an
    ``InstructorRetryException(...) from last_exception``. The category is
    therefore derived from ``exc.__cause__`` first, only falling back to the
    exception itself for the directly re-raised ``IncompleteOutputException``.
    Only parse/validation causes (or a cause-less retry exhaustion) are
    classified as ``invalid_response``; a wrapped 401 stays ``auth_failed``.
    """
    if isinstance(exc, LLMError):
        if isinstance(exc, LLMConfigError):
            return "not_configured"
        if isinstance(exc, LLMNotConfigured):
            return "not_configured"
        for category, error_type in _ERROR_BY_CATEGORY.items():
            if isinstance(exc, error_type):
                return category
        return "invalid_response"

    cause = exc.__cause__ if exc.__cause__ is not None else exc

    # Vendor exception classes are only imported here (lazy) — this function is
    # called from the call path, never from module import.
    try:
        import openai
    except Exception:  # pragma: no cover - openai is a hard dependency
        openai = None  # type: ignore[assignment]

    if openai is not None:
        if isinstance(
            cause, (openai.AuthenticationError, openai.PermissionDeniedError)
        ):
            return "auth_failed"
        if isinstance(cause, openai.NotFoundError):
            return "model_not_found"
        if isinstance(cause, openai.RateLimitError):
            return "rate_limited"
        if isinstance(cause, openai.APITimeoutError):
            return "timeout"
        if isinstance(cause, openai.APIConnectionError):
            return "unreachable"

    try:
        from pydantic import ValidationError

        if isinstance(cause, ValidationError):
            return "invalid_response"
    except Exception:  # pragma: no cover - pydantic is a hard dependency
        pass

    for parse_error_name in ("IncompleteOutputException", "ResponseParsingError"):
        parse_error = _lookup_instructor_error(parse_error_name)
        if parse_error is not None and isinstance(cause, parse_error):
            return "invalid_response"

    if isinstance(cause, json.JSONDecodeError):
        return "invalid_response"

    # A cause-less InstructorRetryException means instructor exhausted its
    # re-ask loop without a specific cause => the response was unusable.
    if _is_instructor_retry_exception(exc):
        return "invalid_response"

    return "invalid_response"


def _lookup_instructor_error(name: str) -> Optional[type]:
    try:
        from instructor import core as instructor_core

        return getattr(instructor_core, name, None)
    except Exception:  # pragma: no cover - instructor is a hard dependency
        return None


def _is_instructor_retry_exception(exc: BaseException) -> bool:
    try:
        from instructor.core import InstructorRetryException

        return isinstance(exc, InstructorRetryException)
    except Exception:  # pragma: no cover - instructor is a hard dependency
        return False


def _normalize_exception(exc: BaseException) -> LLMError:
    """Convert any provider/instructor failure into a typed LLMError."""
    if isinstance(exc, LLMError):
        return exc
    category = classify_exception(exc)
    error_type = _ERROR_BY_CATEGORY.get(category, LLMResponseInvalid)
    return error_type(_ERROR_MESSAGES.get(category, "LLM request failed."))


class LLMClient:
    """The Core-authored facade every caller and plugin uses (D-09).

    Never exposes the raw vendor client. ``is_configured()`` is synchronous;
    every call raises a typed error when the client is unusable (D-10).
    """

    def __init__(
        self,
        *,
        config: Optional[ResolvedLLMConfig] = None,
        config_error: Optional[LLMConfigError] = None,
        raw: Any = None,
        structured: Any = None,
    ) -> None:
        self._config = config
        self._config_error = config_error
        self._raw = raw
        self._structured = structured

    # -- non-secret introspection (reused by GET /config/llm in 302-03) ------
    @property
    def provider(self) -> Optional[str]:
        return self._config.provider if self._config else None

    @property
    def model(self) -> Optional[str]:
        return self._config.model if self._config else None

    @property
    def base_url(self) -> Optional[str]:
        return self._config.base_url if self._config else None

    @property
    def mode(self) -> Optional[str]:
        return self._config.mode if self._config else None

    @property
    def api_key_set(self) -> bool:
        return bool(self._config and self._config.api_key)

    @property
    def config_error(self) -> Optional[str]:
        return str(self._config_error) if self._config_error is not None else None

    def is_configured(self) -> bool:
        """False without a key or with an invalid config — never raises."""
        return (
            self._config_error is None
            and self.api_key_set
            and self._structured is not None
        )

    @property
    def status(self) -> str:
        """``misconfigured`` / ``configured`` / ``not_configured`` — never raises."""
        if self._config_error is not None:
            return "misconfigured"
        return "configured" if self.is_configured() else "not_configured"

    # -- call path ----------------------------------------------------------
    async def generate_structured(
        self,
        messages: list[dict],
        response_model: type,
        *,
        max_retries: int = 2,
        temperature: Optional[float] = None,
    ) -> Any:
        """One structured completion returning a ``response_model`` instance."""
        self._ensure_usable()
        kwargs: dict[str, Any] = {
            "response_model": response_model,
            "messages": messages,
            "max_retries": max_retries,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        started = time.monotonic()
        try:
            result = await self._structured.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised typed below
            self._emit_call(duration_ms=_elapsed_ms(started), outcome=classify_exception(exc))
            raise _normalize_exception(exc) from exc
        self._emit_call(
            duration_ms=_elapsed_ms(started),
            outcome="ok",
            usage=getattr(getattr(result, "_raw_response", None), "usage", None),
        )
        return result

    async def complete_text(self, messages: list[dict]) -> str:
        """One plain Chat Completions call returning the assistant text."""
        self._ensure_usable()
        started = time.monotonic()
        try:
            completion = await self._raw.chat.completions.create(
                model=self.model, messages=messages
            )
            text = completion.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - re-raised typed below
            self._emit_call(duration_ms=_elapsed_ms(started), outcome=classify_exception(exc))
            raise _normalize_exception(exc) from exc
        self._emit_call(
            duration_ms=_elapsed_ms(started),
            outcome="ok",
            usage=getattr(completion, "usage", None),
        )
        return text

    # -- internals ----------------------------------------------------------
    def _ensure_usable(self) -> None:
        if self._config_error is not None:
            raise self._config_error
        if not self.api_key_set:
            raise LLMNotConfigured(
                "LLM provider not configured. Set LLM_API_KEY in environment."
            )
        if self._structured is None or self._raw is None:
            raise LLMConfigError("LLM client is not available.")

    def _emit_call(
        self,
        *,
        duration_ms: float,
        outcome: str,
        usage: Any = None,
    ) -> None:
        """Exactly one ``llm``-category record per call (D-11).

        Only non-secret metadata: never the messages, response body or key.
        provider/model/token usage ride as plain extras outside FROZEN_FIELDS,
        matching the ``app/logging/security.py`` convention.
        """
        extra: dict[str, Any] = {
            "event": "llm_call",
            "duration_ms": duration_ms,
            "reason": outcome,
            "llm_provider": self.provider,
            "llm_model": self.model,
        }
        if usage is not None:
            extra["llm_prompt_tokens"] = getattr(usage, "prompt_tokens", None)
            extra["llm_completion_tokens"] = getattr(usage, "completion_tokens", None)
        _logger.info("llm call", extra=extra)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)


def _build_client(
    config: ResolvedLLMConfig, *, transport: Any = None
) -> LLMClient:
    """Construct the vendor client. Vendor imports are lazy (D-15)."""
    import httpx
    import instructor
    from openai import AsyncOpenAI

    mode = getattr(instructor.Mode, _MODE_ATTRIBUTES[config.mode])
    kwargs: dict[str, Any] = {
        "api_key": config.api_key,
        "base_url": config.base_url,
        "max_retries": 0,
        "timeout": REQUEST_TIMEOUT_SECONDS,
    }
    if transport is not None:
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
    raw = AsyncOpenAI(**kwargs)
    structured = instructor.from_openai(raw, mode=mode, model=config.model)
    return LLMClient(config=config, raw=raw, structured=structured)


_singleton: Optional[LLMClient] = None


def get_llm_client(*, transport: Any = None) -> LLMClient:
    """Return the process-wide LLM facade. Never raises on a bad config/key.

    Two explicit modes:
    - ``transport is None``: the single cached process-wide singleton, built
      once from the resolved env config and reused thereafter.
    - ``transport is not None``: a fresh, dedicated client bound to an
      ``httpx.AsyncClient(transport=transport)`` that bypasses the singleton
      cache entirely. An injected/mock transport is never served to a normal
      call, never overwrites the configured client, and no test can reach a
      real provider.

    A missing key yields a facade whose ``is_configured()`` is False and whose
    calls raise ``LLMNotConfigured``; an invalid config yields a facade that
    carries the sanitized ``LLMConfigError`` and whose calls raise it. Neither
    escapes this factory (so routes/services never 500 on a bad env).
    """
    if transport is not None:
        config, error = _resolve_or_error()
        if error is not None:
            return LLMClient(config_error=error)
        if not config.api_key:
            return LLMClient(config=config)
        return _build_client(config, transport=transport)

    global _singleton
    if _singleton is None:
        config, error = _resolve_or_error()
        if error is not None:
            _singleton = LLMClient(config_error=error)
        elif not config.api_key:
            _singleton = LLMClient(config=config)
        else:
            _singleton = _build_client(config)
    return _singleton


def reset_llm_client() -> None:
    """Clear the process singleton (tests; 302-04 relies on this)."""
    global _singleton
    _singleton = None


def _resolve_or_error() -> tuple[Optional[ResolvedLLMConfig], Optional[LLMConfigError]]:
    try:
        return resolve_llm_config(LLMSettings()), None
    except LLMConfigError as exc:
        return None, exc
