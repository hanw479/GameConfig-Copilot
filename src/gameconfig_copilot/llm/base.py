"""Provider-neutral LLM base: timeout, retry, JSON parsing, token/cost stats.

Concrete adapters implement only ``_chat_once`` and ``_health_once``; everything
observable (retry count, token usage, cost, span attributes) is centralized here
so no business call site re-implements it (ADR-008).
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, SecretStr, ValidationError

from ..observability.tracer import Tracer, get_tracer
from .client import (
    LLMConfigurationError,
    LLMError,
    LLMResponse,
    LLMStructuredOutputError,
    Message,
    Messages,
    Usage,
)

__all__ = ["BaseLLMClient", "LLMStats", "normalize_messages"]

_SCHEMA_INSTRUCTION = """You are a structured configuration generator.
Return exactly ONE JSON object that validates against the JSON Schema below.
Rules:
- Output raw JSON only: no Markdown fences, no comments, no prose around it.
- Include every required field and no additional fields.
- Use the exact enum values and string patterns from the schema.

JSON Schema:
{schema}"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass
class LLMStats:
    """Aggregated counters exposed for cost reports and evaluation."""

    requests: int = 0
    failures: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_tokens"] = self.total_tokens
        return data


def normalize_messages(messages: Messages) -> list[Message]:
    """Accept ``Message`` objects or plain dicts; reject str/empty input."""

    if isinstance(messages, (str, bytes)):
        raise TypeError("messages 必须是 Message/字典序列，不能是字符串")
    normalized: list[Message] = []
    for item in messages:
        if isinstance(item, Message):
            normalized.append(item)
        elif isinstance(item, Mapping):
            normalized.append(Message.model_validate(dict(item)))
        else:
            raise TypeError(f"不支持的消息类型：{type(item).__name__}")
    if not normalized:
        raise LLMConfigurationError("messages 不能为空")
    return normalized


def _prepend_system(messages: Sequence[Message], instruction: str) -> list[Message]:
    merged = list(messages)
    if merged and merged[0].role == "system":
        merged[0] = Message.system(f"{merged[0].content}\n\n{instruction}")
        return merged
    return [Message.system(instruction), *merged]


def _parse_json_object(content: str) -> dict[str, Any]:
    """Extract a JSON object from raw model content (fences/prose tolerated)."""

    text = content.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 解析失败：{exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"期望 JSON object，实际为 {type(parsed).__name__}")
    return parsed


class BaseLLMClient(ABC):
    """Retry/timeout/token/cost layer shared by every provider adapter."""

    provider: ClassVar[str] = "base"
    supports_json_mode: ClassVar[bool] = False
    input_price_cny_per_1m: ClassVar[float] = 0.0
    output_price_cny_per_1m: ClassVar[float] = 0.0

    def __init__(
        self,
        *,
        model: str,
        api_key: str | SecretStr,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 1,
        retry_backoff_s: float = 0.5,
        tracer: Tracer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model.strip():
            raise LLMConfigurationError("model 不能为空", provider=self.provider)
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else str(api_key)
        if not secret.strip():
            raise LLMConfigurationError("API key 不能为空", provider=self.provider)
        if timeout_s <= 0:
            raise LLMConfigurationError("timeout_s 必须大于 0", provider=self.provider)
        if max_retries < 0:
            raise LLMConfigurationError("max_retries 不能为负", provider=self.provider)
        self.model = model.strip()
        self.base_url = (base_url or "").strip().rstrip("/") or None
        self.timeout_s = float(timeout_s)
        self.max_retries = max_retries
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        self._api_key = secret
        self._tracer = tracer
        self._sleep = sleep
        self._stats = LLMStats()
        self._stats_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self.provider!r}, model={self.model!r})"

    @property
    def tracer(self) -> Tracer:
        return self._tracer or get_tracer()

    def _usage_attrs(self, *, missing: bool = True) -> dict[str, Any]:
        """Audit-complete usage fields for every ``llm.*`` span (S7-05).

        ``trace_audit`` requires numeric tokens/cost on each ``llm.*`` span. A
        call that fails before a provider response therefore starts with zeros
        and ``usage_missing=True`` instead of omitting the keys; a successful
        call overwrites them in :meth:`_request`.
        """

        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_cny": 0.0,
            "usage_missing": missing,
        }

    # ------------------------------------------------------------------ public
    def chat(
        self,
        messages: Messages,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        normalized = normalize_messages(messages)
        with self.tracer.span(
            "llm.chat",
            attrs={
                "provider": self.provider,
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                **self._usage_attrs(),
            },
        ) as span:
            return self._request(
                normalized,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=False,
                span=span,
            )

    def structured(
        self,
        messages: Messages,
        schema: type[BaseModel] | Mapping[str, Any],
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        normalized = normalize_messages(messages)
        model_type, schema_dict, schema_name = self._resolve_schema(schema)
        instruction = _SCHEMA_INSTRUCTION.format(
            schema=json.dumps(schema_dict, ensure_ascii=False, indent=2)
        )
        augmented = _prepend_system(normalized, instruction)
        with self.tracer.span(
            "llm.structured",
            attrs={
                "provider": self.provider,
                "model": self.model,
                "schema": schema_name,
                "temperature": temperature,
                "json_mode": self.supports_json_mode,
                **self._usage_attrs(),
            },
        ) as span:
            response = self._request(
                augmented,
                temperature=temperature,
                max_tokens=None,
                json_mode=self.supports_json_mode,
                span=span,
            )
            try:
                parsed = _parse_json_object(response.content)
                if model_type is None:
                    return parsed
                validated = model_type.model_validate(parsed)
            except ValidationError as exc:
                raise LLMStructuredOutputError(
                    f"模型输出未通过 {schema_name} 校验",
                    provider=self.provider,
                    schema_name=schema_name,
                    raw_content=response.content,
                    validation_detail=str(exc),
                ) from exc
            except ValueError as exc:
                raise LLMStructuredOutputError(
                    f"模型输出不符合 {schema_name} 约定：{exc}",
                    provider=self.provider,
                    schema_name=schema_name,
                    raw_content=response.content,
                    validation_detail=str(exc),
                ) from exc
            return validated.model_dump(mode="json", by_alias=True)

    def health(self) -> bool:
        with self.tracer.span(
            "llm.health",
            attrs={
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url,
                # A reachability probe consumes no generation tokens.
                **self._usage_attrs(missing=False),
            },
        ) as span:
            try:
                healthy = bool(self._health_once())
            except Exception as exc:  # health probes must never raise
                span.record_exception(exc)
                span.set_attr("healthy", False)
                return False
            span.set_attr("healthy", healthy)
            return healthy

    def close(self) -> None:
        self._close()

    def __enter__(self) -> BaseLLMClient:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ stats
    def estimate_cost_cny(self, usage: Usage) -> float:
        """Cost estimate from class-level per-1M-token prices (CNY)."""

        cost = (
            usage.prompt_tokens * self.input_price_cny_per_1m
            + usage.completion_tokens * self.output_price_cny_per_1m
        ) / 1_000_000
        return round(cost, 6)

    def snapshot_stats(self) -> dict[str, Any]:
        with self._stats_lock:
            return self._stats.as_dict()

    def _record_success(self, response: LLMResponse) -> float:
        cost = self.estimate_cost_cny(response.usage)
        with self._stats_lock:
            self._stats.requests += 1
            self._stats.prompt_tokens += response.usage.prompt_tokens
            self._stats.completion_tokens += response.usage.completion_tokens
            self._stats.cost_cny = round(self._stats.cost_cny + cost, 6)
            self._stats.latency_ms += response.latency_ms
        return cost

    def _record_failure(self) -> None:
        with self._stats_lock:
            self._stats.failures += 1

    def _record_retry(self) -> None:
        with self._stats_lock:
            self._stats.retries += 1

    # ------------------------------------------------------------------ inner
    def _request(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
        span: Any,
    ) -> LLMResponse:
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            span.set_attr("attempts", attempt)
            started = time.perf_counter()
            try:
                response = self._chat_once(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except LLMError as exc:
                if not exc.retryable or attempt >= attempts:
                    self._record_failure()
                    span.record_exception(exc)
                    span.set_attr("usage_missing_reason", type(exc).__name__)
                    raise
                self._record_retry()
                span.set_attr("retries", attempt)
                self._sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
                continue
            latency_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
            response = response.model_copy(update={"latency_ms": latency_ms})
            cost = self._record_success(response)
            span.update_attrs(
                {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cost_cny": cost,
                    "finish_reason": response.finish_reason,
                    "usage_missing": False,
                }
            )
            return response
        raise LLMError("LLM 请求在重试后仍未执行", provider=self.provider)  # pragma: no cover

    def _resolve_schema(
        self,
        schema: type[BaseModel] | Mapping[str, Any],
    ) -> tuple[type[BaseModel] | None, dict[str, Any], str]:
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return (
                schema,
                schema.model_json_schema(by_alias=True),
                schema.__name__,
            )
        if isinstance(schema, Mapping):
            schema_dict = dict(schema)
            name = str(schema_dict.get("title") or schema_dict.get("$id") or "inline-schema")
            return None, schema_dict, name
        raise LLMConfigurationError(
            "schema 必须是 Pydantic 模型类或 JSON Schema dict",
            provider=self.provider,
        )

    @abstractmethod
    def _chat_once(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool,
    ) -> LLMResponse:
        """Perform exactly one provider call; map transport errors to LLMError."""

    @abstractmethod
    def _health_once(self) -> bool:
        """Perform one reachability probe; may raise (handled by health())."""

    def _close(self) -> None:  # noqa: B027 - optional hook
        """Release provider resources (default no-op)."""
