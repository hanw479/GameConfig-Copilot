"""``gcc search``: hybrid retrieval with snippets, scores and citations (S1-08).

The command is a thin shell: filtering, fusion and ranking live in
``rag/retriever.py`` / ``rag/fusion.py``, so the CLI never touches a storage
backend directly (ADR-007). Output format per hit::

    1. 来源：task:T_SIDE_0213#objectives  分数：0.0161
       任务 T_SIDE_0213「铁匠的委托：失踪的矿车」的目标（objectives）：...

Source refs follow ``provenance.evidence`` (``wiki:<file>#<section>`` /
``task:<id>#<field>`` / ``constraint:<id>#<scope>``), so a retrieved snippet can
be copied into a draft citation.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape
from rich.text import Text

from ..rag.index import DEFAULT_INDEX_DIR, IndexLoadError, load_index
from ..rag.retriever import Retriever, build_filters, parse_level_band
from ._common import console, err_console

_SOURCE_TYPES = ("wiki", "task", "constraint")
_TASK_TYPES = ("main", "side", "daily")
_SNIPPET_CHARS = 160


def search(
    query: str = typer.Argument(..., metavar="QUERY", help="检索问题或片段描述。"),
    top_k: int = typer.Option(
        5,
        "--top-k",
        "-k",
        min=1,
        max=15,
        help="返回片段数（RRF 融合最多保留 15 条）。",
    ),
    source_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help="按来源类型过滤：wiki | task | constraint。",
    ),
    level: str | None = typer.Option(
        None,
        "--level",
        "-l",
        help="按等级段过滤（范围重叠匹配），如 6-9。",
    ),
    task_type: str | None = typer.Option(
        None,
        "--task-type",
        help="按任务类型过滤：main | side | daily。",
    ),
    npc_id: str | None = typer.Option(
        None,
        "--npc",
        help="按发布 NPC 过滤，如 NPC_013。",
    ),
    index: Path = typer.Option(
        DEFAULT_INDEX_DIR,
        "--index",
        "-i",
        help="索引目录（默认 out/index）。",
    ),
) -> None:
    """检索配置片段：片段 + 分数 + 可回溯来源。"""

    if source_type is not None and source_type not in _SOURCE_TYPES:
        raise typer.BadParameter(
            f"未知来源类型 {source_type!r}，可选：{' / '.join(_SOURCE_TYPES)}",
            param_hint="--type",
        )
    if task_type is not None and task_type not in _TASK_TYPES:
        raise typer.BadParameter(
            f"未知任务类型 {task_type!r}，可选：{' / '.join(_TASK_TYPES)}",
            param_hint="--task-type",
        )
    try:
        level_band = parse_level_band(level)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--level") from exc

    try:
        rag_index = load_index(index.expanduser())
    except IndexLoadError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc

    retriever = Retriever(rag_index.sparse, rag_index.dense)
    filters = build_filters(
        source_type=source_type,
        task_type=task_type,
        level_band=level_band,
        npc_id=npc_id,
    )
    hits = retriever.search(query, filters=filters, top_k=top_k)

    if not retriever.dense_available:
        err_console.print(
            "[dim]dense 检索未启用（NoopDenseStore），本次结果 = BM25 + RRF。[/dim]"
        )

    filter_text = "、".join(f"{key}={value}" for key, value in filters.items()) or "无"
    if not hits:
        console.print(
            f"未检索到与「{escape(query.strip())}」匹配的片段"
            f"（过滤：{escape(filter_text)}）。"
        )
        return

    console.print(
        f"检索「{escape(query.strip())}」：{len(hits)} 条"
        f"（过滤：{escape(filter_text)}；RRF k=60）",
        soft_wrap=True,
    )
    for rank, hit in enumerate(hits, start=1):
        console.print(
            f"[bold]{rank}.[/bold] 来源：{escape(hit.citation)}"
            f"  分数：[bold]{hit.score:.4f}[/bold]",
            soft_wrap=True,
        )
        snippet = " ".join(hit.text.split())
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[:_SNIPPET_CHARS] + "…"
        console.print(Text(f"   {snippet}", style="dim"), soft_wrap=True)
