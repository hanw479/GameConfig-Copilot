"""``gcc ingest``: build the hybrid retrieval index from the corpus.

Owner: R (docs/06 §5.2). Reads ``data/{wiki,tasks,constraints}`` and writes the
runtime index to ``out/index`` (gitignored) or ``--index``. Re-running without
changes is a no-op, which is how index persistence idempotency is demonstrated.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..rag.index import DEFAULT_INDEX_DIR, build_index
from ._common import console, err_console


def ingest(
    source: Path = typer.Option(
        Path("data"),
        "--source",
        "-s",
        help="语料根目录（含 wiki/ tasks/ constraints/）。",
    ),
    index: Path = typer.Option(
        DEFAULT_INDEX_DIR,
        "--index",
        "-i",
        help="索引输出目录（默认 out/index，已被 .gitignore 忽略）。",
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="忽略已存在的索引，全量重建。",
    ),
    bm25_backend: str = typer.Option(
        "auto",
        "--bm25-backend",
        help="BM25 实现：auto | python | bm25s。",
    ),
) -> None:
    """构建 BM25 + dense（当前为 Noop）混合索引，并写入可复用的 manifest。"""

    source_path = source.expanduser()
    if not source_path.is_dir():
        err_console.print(f"[red]语料目录不存在：{source_path}[/red]")
        raise typer.Exit(code=2)

    try:
        rag_index = build_index(
            source_path,
            index.expanduser(),
            rebuild=rebuild,
            sparse_backend=bm25_backend,
        )
    except (RuntimeError, ValueError) as exc:
        err_console.print(f"[red]索引构建失败：{exc}[/red]")
        raise typer.Exit(code=2) from exc

    stats = rag_index.stats()
    breakdown = " / ".join(
        f"{source_type} {count}" for source_type, count in stats.by_source_type.items()
    )
    if stats.reused:
        console.print(f"[green]索引已是最新[/green]（语料指纹未变）：{stats.index_dir}")
    else:
        console.print(f"[green]索引构建完成[/green]：{stats.index_dir}")
    console.print(
        f"chunk 总数：[bold]{stats.chunk_count}[/bold]"
        + (f"（{breakdown}）" if breakdown else "")
    )
    console.print(
        f"BM25：[bold]{stats.sparse_backend}[/bold]；"
        f"dense：[bold]{stats.dense_backend}[/bold]"
        + ("（降级：仅 BM25 + RRF）" if stats.dense_backend == "noop" else "")
    )
    console.print(f"语料指纹：{stats.corpus_digest[:16]}")
