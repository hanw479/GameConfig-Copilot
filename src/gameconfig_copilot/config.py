"""Runtime configuration for GameConfig Copilot.

Contract
--------
Input
    Environment variables prefixed with ``GCC_`` and, with lower precedence, a
    local ``.env`` file (never committed; see ``.gitignore``).
Output
    A validated, immutable-at-read :class:`Settings` instance.
Failure semantics
    Missing or invalid required values raise :class:`SettingsError` naming the
    offending environment variable. Validation is lazy (first call to
    :func:`get_settings`), not at package import, because key-free commands such
    as ``gcc schema dump`` must stay runnable on a fresh checkout.

Required variables
------------------
``GCC_LLM_API_KEY`` is the only mandatory value. It is stored as a
:class:`~pydantic.SecretStr` so accidental ``repr``/logging never prints it.

Optional variables
------------------
``GCC_LLM_PROVIDER`` (default ``deepseek``), ``GCC_LLM_MODEL`` (default
``deepseek-chat``), ``GCC_LLM_BASE_URL`` (provider default when unset) and
``GCC_LLM_TIMEOUT_S`` (default ``30``).

Backend
-------
``pydantic-settings`` is the canonical backend and the declared dependency. When
it cannot be installed (offline or sandboxed checkout, restricted CI image), the
import falls back to :mod:`gameconfig_copilot._settings_compat`, which implements
the same env-file/override precedence with plain Pydantic. ``SETTINGS_BACKEND``
reports which backend is active so operators never have to guess.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, ValidationError, field_validator

try:  # pragma: no cover - depends on the environment, not on our code
    from pydantic_settings import BaseSettings, SettingsConfigDict

    SETTINGS_BACKEND = "pydantic-settings"
except ModuleNotFoundError:  # pragma: no cover - exercised via subprocess test
    from ._settings_compat import BaseSettings, SettingsConfigDict

    SETTINGS_BACKEND = "builtin-fallback"

__all__ = [
    "SETTINGS_BACKEND",
    "Settings",
    "SettingsError",
    "get_settings",
    "reset_settings_cache",
]


class SettingsError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    """Validated runtime settings.

    Field names map to environment variables by upper-casing and prefixing with
    ``GCC_``: ``llm_api_key`` -> ``GCC_LLM_API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_prefix="GCC_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    llm_provider: str = Field(
        default="deepseek",
        description="Generation provider: 'deepseek' or 'openai_compatible'.",
    )
    llm_model: str = Field(
        default="deepseek-chat",
        description="Model name passed to the provider (override for internal gateways).",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible base URL; provider default is used when unset.",
    )
    llm_api_key: SecretStr = Field(
        description="Provider API key. Required; provide via GCC_LLM_API_KEY or .env.",
    )
    llm_timeout_s: float = Field(
        default=30.0,
        gt=0,
        le=600,
        description="Per-request timeout in seconds for LLM HTTP calls.",
    )

    @field_validator("llm_provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        if not provider:
            raise ValueError("provider must not be empty")
        return provider

    @field_validator("llm_model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        model = value.strip()
        if not model:
            raise ValueError("model must not be empty")
        return model

    @field_validator("llm_base_url")
    @classmethod
    def _normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        base_url = value.strip().rstrip("/")
        return base_url or None

    @field_validator("llm_api_key")
    @classmethod
    def _require_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API key must not be empty")
        return value


def _describe_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        env_name = f"GCC_{location.upper()}" if location else "GCC_*"
        parts.append(f"{env_name}: {error['msg']}")
    return "; ".join(parts) or str(exc)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process.

    Raises
    ------
    SettingsError
        If a required variable is missing/invalid. The message names the exact
        ``GCC_*`` variable so operators can fix it without reading source code.
    """

    try:
        return Settings()
    except ValidationError as exc:
        raise SettingsError(
            "配置缺失或非法，请设置对应环境变量（或写入本地 .env，.env 已被忽略）："
            f"{_describe_validation_error(exc)}"
        ) from exc


def reset_settings_cache() -> None:
    """Clear the memoized settings (used by tests and ``reload`` tooling)."""

    get_settings.cache_clear()


def __getattr__(name: str) -> Any:
    # ``config.settings`` is a lazy convenience alias; importing this module
    # never requires a key. Type checkers should prefer ``get_settings()``.
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
