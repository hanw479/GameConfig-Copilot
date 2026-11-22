"""DeepSeek provider adapter (OpenAI-compatible HTTP, default provider).

Pricing constants are CNY per 1M tokens for ``deepseek-chat`` and are used only
for local cost estimation; they are intentionally easy to update here rather
than in business code.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .openai_compatible import OpenAICompatibleClient

__all__ = ["DeepSeekClient"]


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek chat adapter; defaults match the public API."""

    provider: ClassVar[str] = "deepseek"
    supports_json_mode: ClassVar[bool] = True
    DEFAULT_MODEL: ClassVar[str] = "deepseek-chat"
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.deepseek.com/v1"
    # CNY per 1M tokens (deepseek-chat, cache-miss list price).
    input_price_cny_per_1m: ClassVar[float] = 2.0
    output_price_cny_per_1m: ClassVar[float] = 8.0

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model or self.DEFAULT_MODEL,
            base_url=base_url or self.DEFAULT_BASE_URL,
            **kwargs,
        )
