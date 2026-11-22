"""LLMClient contract tests (S0-08): adapters, retry, tokens, cost, mock HTTP."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from gameconfig_copilot.config import Settings
from gameconfig_copilot.llm import (
    LLMAuthError,
    LLMClient,
    LLMConfigurationError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMServerError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    Message,
    Usage,
    create_client,
)
from gameconfig_copilot.llm.providers.deepseek import DeepSeekClient
from gameconfig_copilot.llm.providers.openai_compatible import OpenAICompatibleClient
from gameconfig_copilot.observability.tracer import Tracer

TEST_KEY = "sk-unit-test-0123456789abcdef"


class Answer(BaseModel):
    text: str
    score: int = Field(ge=0)


def _completion(content: str = "hello") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )


def _client(handler: Any, **kwargs: Any) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        api_key=TEST_KEY,
        base_url="https://mock.local/v1",
        model="mock-model",
        timeout_s=2.0,
        retry_backoff_s=0.0,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_chat_success_records_usage_and_redacts_key(tracer: Tracer) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return _completion("你好")

    with _client(handler, tracer=tracer) as client:
        response = client.chat([Message.user("hi")], temperature=0.1, max_tokens=32)

    assert response.content == "你好"
    assert response.model == "mock-model"
    assert response.provider == "openai_compatible"
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 3
    assert response.usage.total_tokens == 8
    assert response.latency_ms >= 0
    assert seen["url"] == "https://mock.local/v1/chat/completions"
    assert seen["headers"]["authorization"] == f"Bearer {TEST_KEY}"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["temperature"] == 0.1
    assert seen["body"]["max_tokens"] == 32

    stats = client.snapshot_stats()
    assert stats["requests"] == 1
    assert stats["prompt_tokens"] == 3
    assert stats["completion_tokens"] == 5
    assert stats["total_tokens"] == 8
    assert stats["failures"] == 0
    assert TEST_KEY not in tracer.path.read_text(encoding="utf-8")


def test_structured_validates_pydantic_model_and_injects_schema() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _completion('```json\n{"text": "ok", "score": 3}\n```')

    with _client(handler) as client:
        result = client.structured([Message.user("answer")], Answer)

    assert result == {"text": "ok", "score": 3}
    messages = seen["body"]["messages"]
    assert messages[0]["role"] == "system"
    assert "JSON Schema" in messages[0]["content"]
    assert "Answer" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "answer"}
    # Generic gateways default to prompt-injected schema, not response_format.
    assert "response_format" not in seen["body"]


def test_deepseek_adapter_uses_json_mode_and_default_model() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion('{"text": "ok", "score": 0}')

    with DeepSeekClient(
        api_key=TEST_KEY,
        base_url="https://mock.local/v1",
        transport=httpx.MockTransport(handler),
        retry_backoff_s=0.0,
    ) as client:
        assert client.structured([Message.user("answer")], Answer) == {"text": "ok", "score": 0}

    assert seen["response_format"] == {"type": "json_object"}
    assert seen["model"] == "deepseek-chat"


def test_structured_invalid_json_raises_typed_error() -> None:
    with (
        _client(lambda request: _completion("not-json")) as client,
        pytest.raises(LLMStructuredOutputError) as excinfo,
    ):
        client.structured([Message.user("answer")], Answer)

    error = excinfo.value
    assert error.schema_name == "Answer"
    assert error.raw_content == "not-json"
    assert "JSON" in str(error)


def test_structured_schema_violation_raises_typed_error() -> None:
    with (
        _client(lambda request: _completion('{"text": "ok"}')) as client,
        pytest.raises(LLMStructuredOutputError) as excinfo,
    ):
        client.structured([Message.user("answer")], Answer)

    assert excinfo.value.schema_name == "Answer"
    assert "未通过" in str(excinfo.value)


def test_server_error_is_retried_then_succeeds(tracer: Tracer) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, text="server down")
        return _completion("recovered")

    with _client(handler, max_retries=1, tracer=tracer) as client:
        assert client.chat([Message.user("hi")]).content == "recovered"
        stats = client.snapshot_stats()

    assert len(attempts) == 2
    assert stats["retries"] == 1
    assert stats["failures"] == 0
    assert stats["requests"] == 1


def test_retries_are_exhausted() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="still down")

    with _client(handler, max_retries=1) as client:
        with pytest.raises(LLMServerError):
            client.chat([Message.user("hi")])
        stats = client.snapshot_stats()

    assert len(attempts) == 2
    assert stats["failures"] == 1
    assert stats["requests"] == 0


def test_auth_error_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, text="bad key")

    with _client(handler, max_retries=3) as client, pytest.raises(LLMAuthError):
        client.chat([Message.user("hi")])

    assert len(attempts) == 1


def test_rate_limit_is_retryable() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, text="slow down")
        return _completion("ok")

    with _client(handler, max_retries=1) as client:
        assert client.chat([Message.user("hi")]).content == "ok"

    assert len(attempts) == 2


def test_rate_limit_error_is_marked_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with _client(handler, max_retries=0) as client, pytest.raises(LLMRateLimitError):
        client.chat([Message.user("hi")])

    assert LLMRateLimitError.retryable is True


def test_timeout_is_wrapped_and_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    with _client(handler, max_retries=1) as client, pytest.raises(LLMTimeoutError):
        client.chat([Message.user("hi")])

    assert len(attempts) == 2


def test_connection_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with _client(handler, max_retries=0) as client, pytest.raises(LLMConnectionError):
        client.chat([Message.user("hi")])


def test_health_true_false_and_error() -> None:
    def healthy(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://mock.local/v1/models"
        return httpx.Response(200, json={"data": []})

    with _client(healthy) as client:
        assert client.health() is True

    with _client(lambda request: httpx.Response(503, text="down")) as client:
        assert client.health() is False

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with _client(broken) as client:
        assert client.health() is False


def test_both_adapters_implement_protocol() -> None:
    deep = DeepSeekClient(
        api_key=TEST_KEY,
        transport=httpx.MockTransport(lambda request: _completion()),
    )
    generic = OpenAICompatibleClient(
        api_key=TEST_KEY,
        base_url="https://gateway.local/v1",
        model="internal-7b",
        transport=httpx.MockTransport(lambda request: _completion()),
    )
    try:
        assert isinstance(deep, LLMClient)
        assert isinstance(generic, LLMClient)
        assert deep.provider == "deepseek"
        assert deep.base_url == "https://api.deepseek.com/v1"
        assert generic.provider == "openai_compatible"
        assert generic.base_url == "https://gateway.local/v1"
    finally:
        deep.close()
        generic.close()


def test_factory_selects_provider_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_model="internal-7b",
        llm_base_url="https://gateway.local/v1",
        llm_api_key=TEST_KEY,
    )
    client = create_client(settings)
    try:
        assert isinstance(client, OpenAICompatibleClient)
        assert client.model == "internal-7b"
        assert client.base_url == "https://gateway.local/v1"
    finally:
        client.close()


def test_factory_rejects_unknown_provider() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="mystery-vendor",
        llm_api_key=TEST_KEY,
    )
    with pytest.raises(LLMConfigurationError, match="mystery-vendor"):
        create_client(settings)


def test_openai_compatible_requires_base_url() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_model="internal-7b",
        llm_api_key=TEST_KEY,
    )
    with pytest.raises(LLMConfigurationError, match="GCC_LLM_BASE_URL"):
        create_client(settings)


def test_cost_estimate_uses_provider_pricing() -> None:
    client = DeepSeekClient(
        api_key=TEST_KEY,
        transport=httpx.MockTransport(lambda request: _completion()),
    )
    try:
        usage = Usage(prompt_tokens=1_000_000, completion_tokens=500_000)
        assert client.estimate_cost_cny(usage) == 6.0
    finally:
        client.close()


def test_error_message_redacts_api_key() -> None:
    secret = "sk-" + "z9Y8x7W6" * 4

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"upstream rejected {secret}")

    with _client(handler, max_retries=0) as client, pytest.raises(LLMServerError) as excinfo:
        client.chat([Message.user("hi")])

    assert secret not in str(excinfo.value)
