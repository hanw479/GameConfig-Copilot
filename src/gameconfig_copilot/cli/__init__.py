"""``gcc`` CLI entry point (thin shell only in S0).

Command modules are split by owner (docs/06 §5.2): ``schema`` belongs to
Foundation F; ``ingest``/``search``/``ask``/``eval``/``memory`` will be filled
by their respective workers in later waves. Every not-yet-implemented command
exits with code 2 and a friendly, side-effect-free message.
"""

from __future__ import annotations

import contextlib
import sys

import typer

from .ask import ask
from .eval import evaluate
from .ingest import ingest
from .memory import memory
from .schema import app as schema_app
from .search import search

app = typer.Typer(
    name="gcc",
    help=(
        "GameConfig Copilot 命令行入口。\n\n"
        "当前为 S0 地基：`schema dump` 可用，其余业务子命令为占位。"
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
)

app.add_typer(schema_app, name="schema", help="导出/查看数据契约 JSON Schema。")
app.command("ingest")(ingest)
app.command("search")(search)
app.command("ask")(ask)
app.command("eval")(evaluate)
app.command("memory")(memory)


def _configure_stdio() -> None:
    """Emit UTF-8 even when stdout is a pipe on a non-UTF-8 code page.

    ``gcc schema dump`` is meant to be piped (``| jq .``), and the payload
    contains Chinese descriptions. Without this, Windows falls back to the ANSI
    code page (cp936 here) and the JSON is not valid UTF-8 for the next program.
    Real consoles already speak UTF-8 via ``_WindowsConsoleIO``, so this mainly
    affects redirects and pipes.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - exotic/captured streams
            continue
        with contextlib.suppress(OSError, ValueError):  # pragma: no cover - defensive
            reconfigure(encoding="utf-8")


def main() -> None:
    """Console-script entry point (``gcc``)."""

    _configure_stdio()
    app()


__all__ = ["app", "main"]

# ``python -m gameconfig_copilot.cli`` is served by ``cli/__main__.py``.
