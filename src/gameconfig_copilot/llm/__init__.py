"""Generation layer: ``LLMClient`` Protocol, response types and provider factory.

Business code imports ``create_client`` (or just the Protocol) from here and
never touches a vendor SDK or HTTP payload.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings, get_settings
from .base import BaseLLMClient, LLMStats
from .client import (
    LLMAuthError,
    LLMClient,
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseError,
    LLMServerError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    Message,
    Messages,
    Role,
    Usage,
)
from .instrumented import TracedLLM, wrap_llm

__all__ = [
    "BaseLLMClient",
    "LLMAuthError",
    "LLMClient",
    "LLMConfigurationError",
    "LLMConnectionError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMResponseError",
    "LLMServerError",
    "LLMStats",
    "LLMStructuredOutputError",
    "LLMTimeoutError",
    "Message",
    "Messages",
    "Role",
    "TracedLLM",
    "Usage",
    "create_client",
    "wrap_llm",
]


def create_client(settings: Settings | None = None, **overrides: Any) -> BaseLLMClient:
    """Build the configured provider adapter.

    ``GCC_LLM_PROVIDER`` selects the adapter; callers can override constructor
    kwargs (``model``, ``base_url``, ``timeout_s``, ``transport`` ... ) without
    changing business code.

    Raises
    ------
    LLMConfigurationError
        Unknown provider or missing ``base_url`` for ``openai_compatible``.
    """

    resolved = settings or get_settings()
    provider = resolved.llm_provider
    options: dict[str, Any] = {
        "api_key": resolved.llm_api_key.get_secret_value(),
        "model": resolved.llm_model,
        "base_url": resolved.llm_base_url,
        "timeout_s": resolved.llm_timeout_s,
    }
    options.update(overrides)

    if provider == "deepseek":
        from .providers.deepseek import DeepSeekClient

        return DeepSeekClient(**options)

    if provider in {"openai_compatible", "openai", "vllm"}:
        from .providers.openai_compatible import OpenAICompatibleClient

        return OpenAICompatibleClient(**options)

    raise LLMConfigurationError(
        f"未知的 LLM provider {provider!r}；可用：deepseek, openai_compatible",
        provider=provider,
    )
