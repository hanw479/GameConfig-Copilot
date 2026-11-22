"""Shared CLI helpers (thin shell; no business logic)."""

from __future__ import annotations

from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel

__all__ = ["console", "err_console", "pending"]

console = Console()
err_console = Console(stderr=True)


def pending(command: str, stage: str, detail: str) -> NoReturn:
    """Print a friendly "not implemented yet" panel and exit with code 2."""

    err_console.print(
        Panel(
            f"{detail}\n\n"
            f"[dim]计划交付阶段：{stage}[/dim]\n"
            "[dim]本命令尚未实现，不会创建、修改或删除任何文件。[/dim]",
            title=f"gcc {command} · 尚未实现",
            border_style="yellow",
            padding=(0, 1),
        )
    )
    raise typer.Exit(code=2)
