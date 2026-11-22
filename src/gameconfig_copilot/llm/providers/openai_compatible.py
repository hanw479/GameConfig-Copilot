"""OpenAI-compatible HTTP adapter (internal vLLM / gateway / OpenAI).

The vendor SDK restriction is moot here: this adapter speaks the HTTP protocol
with ``httpx`` and never imports an OpenAI/deepseek SDK. It is the only place
besides :mod:`gameconfig_copilot.llm.providers.deepseek` allowed to know about
Chat Completions wire format.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

import httpx

from ...observability.tracer import Tracer
from ..base import BaseLLMClient
from ..client import (
    LLMAuthError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMResponse,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    Message,
    Usage,
)

__all__ = ["OpenAICompatibleClient"]


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class OpenAICompatibleClient(BaseLLMClient):
    """Chat Completions adapter driven by an explicit ``base_url``."""

    provider: ClassVar[str] = "openai_compatible"
    supports_json_mode: ClassVar[bool] = False
    DEFAULT_MODEL: ClassVar[str] = ""
    DEFAULT_BASE_URL: ClassVar[str] = ""
    CHAT_PATH: ClassVar[str] = "/chat/completions"
    MODELS_PATH: ClassVar[str] = "/models"

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 1,
        retry_backoff_s: float = 0.5,
        tracer: Tracer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        supports_json_mode: bool | None = None,
    ) -> None:
        super().__init__(
            model=model or self.DEFAULT_MODEL,
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL or None,
            timeout_s=timeout_s,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
            tracer=tracer,
            sleep=sleep,
        )
        if self.base_url is None:
            raise LLMConfigurationError(
                "openai_compatible 需要显式 base_url（环境变量 GCC_LLM_BASE_URL）",
                provider=self.provider,
            )
        if supports_json_mode is not None:
            self.supports_json_mode = bool(supports_json_mode)
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(self.timeout_s),
            transport=transport,
        )

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if not self.base_url:  # pragma: no cover - guarded in __init__
            raise LLMConfigurationError(
                "缺少 base_url（环境变量 GCC_LLM_BASE_URL）",
                provider=self.provider,
            )
        return f"{self.base_url}{path}"

    def _chat_once(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "temperature": float(temperature),
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._http.post(
                self._url(self.CHAT_PATH),
                json=payload,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"LLM 请求超时（{self.timeout_s:g}s）",
                provider=self.provider,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMConnectionError(
                f"LLM 网络连接失败：{type(exc).__name__}",
                provider=self.provider,
            ) from exc

        self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(
                "LLM 响应不是合法 JSON",
                provider=self.provider,
                status_code=response.status_code,
            ) from exc
        return self._parse_completion(data, response.status_code)

    def _health_once(self) -> bool:
        try:
            response = self._http.get(
                self._url(self.MODELS_PATH),
                headers=self._headers(),
                timeout=min(self.timeout_s, 5.0),
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # ------------------------------------------------------------------ parsing
    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        snippet = response.text[:300] if response.text else ""
        if status in (401, 403):
            raise LLMAuthError(
                f"LLM 认证失败 (HTTP {status})：{snippet}",
                provider=self.provider,
                status_code=status,
            )
        if status == 429:
            raise LLMRateLimitError(
                "LLM 触发限流 (HTTP 429)",
                provider=self.provider,
                status_code=status,
            )
        if status >= 500:
            raise LLMServerError(
                f"LLM 服务端错误 (HTTP {status})：{snippet}",
                provider=self.provider,
                status_code=status,
            )
        raise LLMResponseError(
            f"LLM 请求被拒绝 (HTTP {status})：{snippet}",
            provider=self.provider,
            status_code=status,
        )

    def _parse_completion(self, data: Any, status_code: int) -> LLMResponse:
        if not isinstance(data, dict):
            raise LLMResponseError(
                "LLM 响应结构不是 JSON object",
                provider=self.provider,
                status_code=status_code,
            )
        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError(
                "LLM 响应缺少 choices", provider=self.provider, status_code=status_code
            )
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else ""
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        usage_data = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = Usage(
            prompt_tokens=_as_int(usage_data.get("prompt_tokens")),
            completion_tokens=_as_int(usage_data.get("completion_tokens")),
            total_tokens=_as_int(usage_data.get("total_tokens")),
        )
        finish_reason = choice.get("finish_reason")
        return LLMResponse(
            content=content,
            model=str(data.get("model") or self.model),
            usage=usage,
            finish_reason=None if finish_reason is None else str(finish_reason),
            provider=self.provider,
        )

    def _close(self) -> None:
        if self._owns_http:
            self._http.close()
