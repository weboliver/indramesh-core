"""Typed LLM errors shared by Core callers (D-07/D-10).

Messages deliberately carry no API key and no raw provider body (D-07/D-11):
they are safe to surface through an HTTPException detail and to log.
"""


class LLMError(Exception):
    """Base class for every app/llm failure surfaced to Core callers."""


class LLMConfigError(LLMError):
    """Invalid or incomplete ``LLM_*`` configuration."""


class LLMNotConfigured(LLMError):
    """No ``LLM_API_KEY`` is set — every call is refused (D-10)."""


class LLMAuthFailed(LLMError):
    """The provider rejected the API key (401/403)."""


class LLMUnreachable(LLMError):
    """The provider endpoint could not be reached (network error)."""


class LLMTimeout(LLMError):
    """The provider did not answer within the request timeout."""


class LLMModelNotFound(LLMError):
    """The configured model does not exist on the provider (404)."""


class LLMRateLimited(LLMError):
    """The provider rate-limited the request (429)."""


class LLMResponseInvalid(LLMError):
    """The provider answered, but no valid structured output could be parsed."""
