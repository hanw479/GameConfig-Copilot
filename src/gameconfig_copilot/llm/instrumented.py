"""Trace wrapper for ``LLMClient`` implementations outside ``BaseLLMClient``.

``BaseLLMClient`` already emits ``llm.*`` spans for every provider adapter
(ADR-008). An injected client (test fake, alternate transport) implements the
``LLMClient`` Protocol directly and would otherwise leave the trace without any
generation span, so S7-05 coverage would depend on which adapter is wired.

``wrap_llm`` is applied once where the agent graph is assembled: base adapters
are returned untouched (they trace themselves, avoiding duplicate cost/token
spans), anything else is wrapped by :class:`TracedLLM`.

Input
    Any ``LLMClient``-shaped object plus an optional ``Tracer``.
Output
    The delegate's return values unchanged, plus ``llm.chat`` /
    ``llm.structured`` / ``llm.health`` spans whose attrs always carry
    ``provider`` / ``model`` / ``prompt_tokens`` / ``completion_tokens`` /
    ``cost_cny`` (zeros + ``usage_missing`` when the delegate cannot report
    usage).
Failure semantics
    Exceptions are recorded on the span and re-raised unchanged; instrumentation
    never swallows or rewrites a generation error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from ..observability.tracer import Span, Tracer, get_tracer
from .base import BaseLLMClient
from .client import LLMResponse, Messages

__all__ = ["TracedLLM", "wrap_llm"]

_ZERO_USAGE: dict[str, Any] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cost_cny": 0.0,
    "usage_missing": True,
}


def _zero_usage(*, missing: bool = True) -> dict[str, Any]:
    return {**_ZERO_USAGE, "usage_missing": missing}


def _provider_name(llm: Any) -> str:
    """Provider label: explicit attribute first, class name as fallback."""

    provider = getattr(llm, "provider", None)
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    name = type(llm).__name__
    for suffix in ("Client", "LLM", "Llm"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower() or "unknown"


def _schema_name(schema: type[BaseModel] | Mapping[str, Any]) -> str:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.__name__
    if isinstance(schema, Mapping):
        title = schema.get("title") or schema.get("$id")
        return str(title) if title else "inline-schema"
    return type(schema).__name__


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class TracedLLM:
    """Delegate to any ``LLMClient`` while emitting ``llm.*`` spans."""

    def __init__(self, llm: Any, *, tracer: Tracer | None = None) -> None:
        missing = [
            name
            for name in ("chat", "structured", "health")
            if not callable(getattr(llm, name, None))
        ]
        if missing:
            raise TypeError(f"LLMClient 缺少方法：{', '.join(missing)}")
        self._llm = llm
        self._tracer = tracer

    def __repr__(self) -> str:
        return f"TracedLLM(provider={self.provider!r}, delegate={self._llm!r})"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    @property
    def tracer(self) -> Tracer:
        return self._tracer or get_tracer()

    @property
    def provider(self) -> str:
        return _provider_name(self._llm)

    @property
    def model(self) -> str:
        return str(getattr(self._llm, "model", "") or "")

    # ------------------------------------------------------------------ public
    def chat(
        self,
        messages: Messages,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        with self.tracer.span(
            "llm.chat",
            attrs={
                "provider": self.provider,
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                **_zero_usage(),
            },
        ) as span:
            try:
                response = self._llm.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                span.set_attr("usage_missing_reason", type(exc).__name__)
                raise
            self._record_usage(span, getattr(response, "usage", None))
            return response

    def structured(
        self,
        messages: Messages,
        schema: type[BaseModel] | Mapping[str, Any],
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        with self.tracer.span(
            "llm.structured",
            attrs={
                "provider": self.provider,
                "model": self.model,
                "schema": _schema_name(schema),
                "temperature": temperature,
                # Protocol-level ``structured`` returns only the payload, so the
                # delegate cannot report usage; keep numeric zeros + a flag.
                **_zero_usage(),
            },
        ) as span:
            payload = self._llm.structured(messages, schema, temperature=temperature)
            span.set_attr("usage_missing_reason", "structured_payload_has_no_usage")
            return payload

    def health(self) -> bool:
        with self.tracer.span(
            "llm.health",
            attrs={
                "provider": self.provider,
                "model": self.model,
                **_zero_usage(missing=False),
            },
        ) as span:
            healthy = bool(self._llm.health())
            span.set_attr("healthy", healthy)
            return healthy

    # ----------------------------------------------------------------- inner
    def _record_usage(self, span: Span, usage: Any) -> None:
        prompt = _count(getattr(usage, "prompt_tokens", None))
        completion = _count(getattr(usage, "completion_tokens", None))
        if prompt is None or completion is None:
            span.set_attr("usage_missing_reason", "response_usage_missing")
            return
        total = _count(getattr(usage, "total_tokens", None))
        span.update_attrs(
            {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total if total is not None else prompt + completion,
                "cost_cny": self._cost_cny(usage),
                "usage_missing": False,
            }
        )

    def _cost_cny(self, usage: Any) -> float:
        estimate = getattr(self._llm, "estimate_cost_cny", None)
        if callable(estimate) and usage is not None:
            try:
                return round(float(estimate(usage)), 6)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                return 0.0
        input_price = getattr(self._llm, "input_price_cny_per_1m", None)
        output_price = getattr(self._llm, "output_price_cny_per_1m", None)
        if isinstance(input_price, (int, float)) and isinstance(output_price, (int, float)):
            prompt = _count(getattr(usage, "prompt_tokens", None)) or 0
            completion = _count(getattr(usage, "completion_tokens", None)) or 0
            return round(
                (prompt * float(input_price) + completion * float(output_price))
                / 1_000_000,
                6,
            )
        return 0.0


def wrap_llm(llm: Any, *, tracer: Tracer | None = None) -> Any:
    """Wrap a non-base ``LLMClient``; base adapters already trace themselves."""

    if llm is None or isinstance(llm, (BaseLLMClient, TracedLLM)):
        return llm
    return TracedLLM(llm, tracer=tracer)
