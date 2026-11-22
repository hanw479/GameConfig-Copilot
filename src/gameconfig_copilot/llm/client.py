"""Generation-model contract: ``LLMClient`` Protocol + response/error types.

Business layers (``agents/``, ``evaluation/``) may import **only** this module
and ``llm.base`` factories; vendor SDKs/HTTP details live in ``llm/providers/``
(ADR-008, docs/06 §5.5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..observability.tracer import redact_text

__all__ = [
    "LLMAuthError",
    "LLMClient",
    "LLMConfigurationError",
    "LLMConnectionError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMResponseError",
    "LLMServerError",
    "LLMStructuredOutputError",
    "LLMTimeoutError",
    "Message",
    "Messages",
    "Role",
    "Usage",
]

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    """One chat message in provider-neutral form."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role="assistant", content=content)


Messages = Sequence[Message | Mapping[str, Any]]


class Usage(BaseModel):
    """Token accounting normalized across providers."""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _fill_total(self) -> Usage:
        expected = self.prompt_tokens + self.completion_tokens
        if self.total_tokens == 0 and expected > 0:
            self.total_tokens = expected
        return self


class LLMResponse(BaseModel):
    """Normalized completion returned by every provider adapter."""

    model_config = ConfigDict(extra="forbid")

    content: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = Field(default=0, ge=0)
    finish_reason: str | None = None
    provider: str = ""


class LLMError(RuntimeError):
    """Base class for all LLM-layer failures (never leaks API keys)."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(redact_text(message))
        self.provider = provider
        self.status_code = status_code


class LLMConfigurationError(LLMError):
    """Missing/invalid provider configuration (bad base URL, unknown provider)."""


class LLMTimeoutError(LLMError):
    retryable = True


class LLMConnectionError(LLMError):
    retryable = True


class LLMRateLimitError(LLMError):
    retryable = True


class LLMServerError(LLMError):
    retryable = True


class LLMAuthError(LLMError):
    """401/403: retrying with the same key cannot help."""


class LLMResponseError(LLMError):
    """Provider returned a malformed or non-retryable response."""


class LLMStructuredOutputError(LLMError):
    """Model output was not valid JSON or did not satisfy the target schema.

    The TaskAgent is expected to retry once with the validation detail
    (docs/03 S3-02); this exception carries enough context to build that repair
    prompt without exposing secrets.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        schema_name: str = "",
        raw_content: str = "",
        validation_detail: str = "",
    ) -> None:
        super().__init__(message, provider=provider)
        self.schema_name = schema_name
        self.raw_content = redact_text(raw_content)[:500]
        self.validation_detail = redact_text(validation_detail)[:1000]


@runtime_checkable
class LLMClient(Protocol):
    """The only generation entry point business code is allowed to depend on."""

    def chat(
        self,
        messages: Messages,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Free-form completion with timeout/retry/token accounting."""

    def structured(
        self,
        messages: Messages,
        schema: type[BaseModel] | Mapping[str, Any],
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Return a dict validated against ``schema``.

        Raises
        ------
        LLMStructuredOutputError
            When the model output cannot be parsed/validated.
        """

    def health(self) -> bool:
        """Cheap provider reachability probe; never raises."""
