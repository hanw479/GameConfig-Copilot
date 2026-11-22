"""Provider adapters. Only this package may know vendor HTTP/SDK details."""

from __future__ import annotations

from .deepseek import DeepSeekClient
from .openai_compatible import OpenAICompatibleClient

__all__ = ["DeepSeekClient", "OpenAICompatibleClient"]
