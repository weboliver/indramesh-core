"""Provider-neutral LLM client package (D-14/D-15).

One OpenAI-compatible Chat Completions client shared by the existing Core AI
services and every future AI plugin. The public surface is deliberately
Core-typed: plugins reach it through ``ctx.llm`` and never see ``instructor``
or ``openai``.
"""

from app.llm.client import (
    LLMClient,
    classify_exception,
    get_llm_client,
    reset_llm_client,
)
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
    MODE_STRINGS,
    PROVIDER_PRESETS,
    LLMSettings,
    ProviderPreset,
    ResolvedLLMConfig,
    resolve_llm_config,
)

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMConfigError",
    "LLMNotConfigured",
    "LLMAuthFailed",
    "LLMUnreachable",
    "LLMTimeout",
    "LLMModelNotFound",
    "LLMRateLimited",
    "LLMResponseInvalid",
    "LLMSettings",
    "ProviderPreset",
    "ResolvedLLMConfig",
    "PROVIDER_PRESETS",
    "MODE_STRINGS",
    "resolve_llm_config",
    "classify_exception",
    "get_llm_client",
    "reset_llm_client",
]
