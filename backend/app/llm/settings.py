"""LLM settings and provider preset resolution (D-01..D-04).

Vendor-free by design: this module imports no ``instructor``/``openai``/
``mistralai``. The structured mode is kept as a plain string from a small
vocabulary; ``client.py`` is the only place that maps it onto an instructor
``Mode`` (D-15 — ``from app.config import settings`` must not pull a vendor SDK).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic_settings import BaseSettings

from app.llm.errors import LLMConfigError


@dataclass(frozen=True)
class ProviderPreset:
    """Default base URL and structured mode for one known provider."""

    base_url: Optional[str]
    mode: str


# D-01/D-02/D-03: a preset supplies both the base URL and the structured mode;
# an explicit LLM_BASE_URL / LLM_MODE always wins over the preset.
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "mistral": ProviderPreset(
        base_url="https://api.mistral.ai/v1", mode="json_schema"
    ),
    "openai": ProviderPreset(
        base_url="https://api.openai.com/v1", mode="tools"
    ),
    "openai_compatible": ProviderPreset(base_url=None, mode="json_schema"),
}

# The exact LLM_MODE vocabulary (D-03). The mapping to instructor modes lives
# in client.py so this module stays vendor-free.
MODE_STRINGS = ("tools", "json_schema", "json", "md_json")


class LLMSettings(BaseSettings):
    """The five-value LLM env contract (D-01).

    Read from the process environment with no prefix and case-insensitively,
    mirroring ``AppSettings``/``StorageSettings`` in ``app.config``. There is
    deliberately no default model (D-01/discretion): a missing ``LLM_MODEL``
    is a configuration error, never a silent default.
    """

    llm_provider: str = "mistral"
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_mode: Optional[str] = None

    model_config = {"env_prefix": "", "case_sensitive": False}


@dataclass(frozen=True)
class ResolvedLLMConfig:
    """The resolved, validated LLM configuration (api_key included — internal)."""

    provider: str
    model: Optional[str]
    base_url: Optional[str]
    mode: str
    api_key: str


def resolve_llm_config(settings: LLMSettings) -> ResolvedLLMConfig:
    """Resolve the provider preset plus explicit overrides into a config.

    Raises ``LLMConfigError`` loudly (D-04) rather than guessing:
    - an unknown ``LLM_PROVIDER`` (a typo must not silently fall back),
    - a legacy ``mistral/...`` ``LLM_MODEL`` prefix,
    - ``openai_compatible`` without an explicit ``LLM_BASE_URL``,
    - an ``LLM_MODE`` outside the vocabulary,
    - a key set with no ``LLM_MODEL`` (no silent default model).
    """
    provider = (settings.llm_provider or "").strip() or "mistral"
    if provider not in PROVIDER_PRESETS:
        allowed = ", ".join(sorted(PROVIDER_PRESETS))
        raise LLMConfigError(
            f"Unknown LLM_PROVIDER '{provider}'. Allowed values: {allowed}."
        )
    preset = PROVIDER_PRESETS[provider]

    model = (settings.llm_model or "").strip() or None
    if model is not None and model.startswith("mistral/"):
        raise LLMConfigError(
            "LLM_MODEL must be a bare model id (e.g. 'mistral-small-latest'); "
            "remove the legacy 'mistral/' prefix."
        )

    api_key = (settings.llm_api_key or "").strip()

    base_url = (settings.llm_base_url or "").strip() or preset.base_url
    if provider == "openai_compatible" and not base_url:
        raise LLMConfigError(
            "LLM_BASE_URL is required when LLM_PROVIDER=openai_compatible."
        )

    mode = (settings.llm_mode or "").strip() or preset.mode
    if mode not in MODE_STRINGS:
        allowed = ", ".join(MODE_STRINGS)
        raise LLMConfigError(
            f"LLM_MODE '{mode}' is invalid. Allowed values: {allowed}."
        )

    if api_key and not model:
        raise LLMConfigError(
            "LLM_MODEL is not set. Set LLM_MODEL to a bare model id."
        )

    return ResolvedLLMConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        mode=mode,
        api_key=api_key,
    )
