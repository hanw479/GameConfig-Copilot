"""``gcc schema``: dump/read the frozen data contracts.

``schema dump`` is the one S0 command that must work on a fresh checkout
without any configuration or API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..schemas import (
    available_schemas,
    dump_json_schema,
    export_all_schemas,
    resolve_schema_name,
)
from ._common import console, err_console

app = typer.Typer(
    help="查看/导出 TaskDraft、ValidationReport、Constraint 的 JSON Schema。",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.command("dump")
def dump(
    name: str = typer.Option(
        "task",
        "--name",
        "-n",
        help="schema 名称：task | report | constraint | all。",
    ),
    indent: int = typer.Option(2, "--indent", min=0, max=8, help="缩进空格数。"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="写入文件而不是打印到 stdout。",
    ),
) -> None:
    """输出合法 JSON Schema（默认 task）。"""

    normalized = name.strip().lower()
    if normalized == "all":
        payload: object = export_all_schemas()
        text = json.dumps(payload, ensure_ascii=False, indent=indent)
    else:
        try:
            resolve_schema_name(normalized)
            text = dump_json_schema(normalized, indent=indent)
        except KeyError as exc:
            err_console.print(f"[red]{exc.args[0]}[/red]")
            raise typer.Exit(code=2) from exc

    if output is not None:
        target = output.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        err_console.print(f"[green]已写入[/green] {target}")
        return
    # Plain JSON on stdout so the command is pipeable (`gcc schema dump | jq .`).
    typer.echo(text)


@app.command("list")
def list_schemas() -> None:
    """列出可导出的 schema 名称。"""

    for schema_name in available_schemas():
        console.print(f"[bold]{schema_name}[/bold]")
