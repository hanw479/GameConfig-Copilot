"""``gcc memory``: inspect and clear designer preference memory (S6-02).

The command is a thin shell over :mod:`gameconfig_copilot.memory.preference`
(ADR-007): ``show`` renders a designer's learned defaults, provenance
(``sources``) and pending-confirmation queue; ``clear`` deletes that designer's
record and refuses to run without ``--yes``. Storage is the default
``JsonPreferenceStore`` under ``data/memory/preferences/`` (overridable with
``--root`` so tests and alternative deployments never touch the real store).

Usage::

    gcc memory show  --designer u_1024            # human-readable
    gcc memory show  --designer u_1024 --json     # pipeable JSON
    gcc memory clear --designer u_1024 --yes      # delete after confirmation
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.markup import escape

from ..memory.preference import (
    DEFAULT_PREFERENCE_ROOT,
    PREFERENCE_FIELDS,
    PreferenceError,
    PreferenceStore,
    open_preference_store,
)
from ._common import console, err_console

_ACTIONS = ("show", "clear")


def memory(
    action: str = typer.Argument(
        "show",
        metavar="ACTION",
        help="操作：show（查看偏好/来源/待确认队列）| clear（清除该策划的偏好）。",
    ),
    designer: str | None = typer.Option(
        None,
        "--designer",
        "--designer-id",
        "-d",
        help="策划 ID（必填），如 u_1024。",
    ),
    root: str | None = typer.Option(
        None,
        "--root",
        help=f"偏好存储目录（默认 {DEFAULT_PREFERENCE_ROOT}）。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON（只写 stdout，便于管道传给 jq）。",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="clear 的显式确认；缺省时本命令不会删除任何数据。",
    ),
) -> None:
    """查看/清除分层记忆中的策划偏好（默认 JSON 存储，Redis 可插拔）。"""

    normalized = (action or "").strip().lower()
    if normalized not in _ACTIONS:
        raise typer.BadParameter(
            f"未知操作 {action!r}，可选：{' / '.join(_ACTIONS)}",
            param_hint="ACTION",
        )
    designer_id = (designer or "").strip()
    if not designer_id:
        err_console.print(
            "[red]缺少 --designer：请指定策划 ID，例如 "
            "`gcc memory show --designer u_1024`。[/red]"
        )
        raise typer.Exit(code=2)

    try:
        store = open_preference_store("json", root=root)
        if normalized == "show":
            _show(store, designer_id, as_json=json_output)
        else:
            _clear(store, designer_id, as_json=json_output, yes=yes)
    except PreferenceError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc


def _format_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _show(store: PreferenceStore, designer_id: str, *, as_json: bool) -> None:
    payload = store.show(designer_id)
    if as_json:
        typer.echo(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
        return

    state = (
        "已过期（超过 TTL，读取时按空偏好处理）" if payload["expired"] else "有效"
    )
    updated_at = payload.get("updated_at")
    console.print(
        f"[bold]策划[/bold]：{escape(str(payload['designer_id']))}  "
        f"[bold]后端[/bold]：{escape(str(payload['backend']))}  "
        f"[bold]状态[/bold]：{state}",
        soft_wrap=True,
    )
    console.print(f"[bold]位置[/bold]：{escape(str(payload['location']))}", soft_wrap=True)
    console.print(
        "[bold]更新时间[/bold]："
        f"{escape(str(updated_at)) if updated_at else '（尚未写入）'}",
        soft_wrap=True,
    )

    defaults = payload.get("defaults") or {}
    console.print("[bold]偏好默认值[/bold]", soft_wrap=True)
    if not defaults:
        console.print(
            "  [dim]（尚无偏好：该策划还没有积累可用的默认值）[/dim]", soft_wrap=True
        )
    else:
        for name in PREFERENCE_FIELDS:
            if name in defaults:
                console.print(
                    f"  {name}：{escape(_format_value(defaults[name]))}",
                    soft_wrap=True,
                )

    sources = payload.get("sources") or []
    console.print(f"[bold]来源记录[/bold]（{len(sources)} 条）", soft_wrap=True)
    if not sources:
        console.print("  [dim]（无来源记录）[/dim]", soft_wrap=True)
    else:
        for index, source in enumerate(sources, start=1):
            console.print(
                f"  {index}. {escape(str(source.get('field', '?')))}："
                f"{escape(_format_value(source.get('old')))} → "
                f"{escape(_format_value(source.get('new')))}"
                f"（来源提交 {escape(str(source.get('source_commit', '-')))}"
                f"，{escape(str(source.get('updated_at', '-')))}）",
                soft_wrap=True,
            )

    pending = payload.get("pending") or []
    console.print(f"[bold]待确认队列[/bold]（{len(pending)} 条）", soft_wrap=True)
    if not pending:
        console.print("  [dim]（无待确认样本）[/dim]", soft_wrap=True)
    else:
        for item in pending:
            console.print(
                f"  - {escape(str(item.get('id', '?')))}："
                f"{escape(str(item.get('field', '?')))}="
                f"{escape(_format_value(item.get('value')))}"
                f"（均值 {escape(str(item.get('mean', '?')))}，"
                f"σ {escape(str(item.get('sigma', '?')))}"
                f"，来源提交 {escape(str(item.get('source_commit', '-')))}"
                f"，观察于 {escape(str(item.get('observed_at', '-')))}）",
                soft_wrap=True,
            )
            if item.get("reason"):
                console.print(
                    f"    原因：{escape(str(item['reason']))}", soft_wrap=True
                )


def _clear(
    store: PreferenceStore,
    designer_id: str,
    *,
    as_json: bool,
    yes: bool,
) -> None:
    location = store.location(designer_id)
    if not yes:
        payload = {
            "designer_id": designer_id,
            "cleared": False,
            "location": location,
            "reason": "confirmation_required",
        }
        if as_json:
            typer.echo(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
        else:
            console.print(
                f"[yellow]清除策划 {escape(designer_id)} 的偏好记录需要显式确认："
                "请在命令后加 --yes（本次未删除任何数据）。[/yellow]",
                soft_wrap=True,
            )
        raise typer.Exit(code=2)

    removed = store.clear(designer_id)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "designer_id": designer_id,
                    "cleared": removed,
                    "location": location,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    if removed:
        console.print(
            f"[green]已清除[/green] 策划 {escape(designer_id)} 的偏好记录"
            f"（{escape(location)}）。",
            soft_wrap=True,
        )
    else:
        console.print(
            f"没有找到策划 {escape(designer_id)} 的偏好记录"
            f"（{escape(location)}），未删除任何数据。",
            soft_wrap=True,
        )
