"""HTTP/SSE layer (Worker A). Import ``create_app`` lazily to keep imports light."""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Delegate to :func:`gameconfig_copilot.api.main.create_app`."""

    from .main import create_app as factory

    return factory(*args, **kwargs)
