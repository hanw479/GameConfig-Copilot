"""Corpus loader: ``data/{wiki,tasks,constraints}`` -> neutral chunks.

Only this module knows the on-disk corpus layout. Files are processed in sorted
order and chunk ids are deterministic, which is what makes repeated index builds
byte-identical (docs/03 S1-05 acceptance: 索引持久化幂等).

Input
    Corpus root (default ``data/``) containing ``wiki/*.md``,
    ``tasks/*.json`` and ``constraints/*.yaml``.
Output
    Deduplicated ``list[Chunk]`` in deterministic order.
Failure semantics
    Missing root/malformed JSON/YAML/duplicate chunk ids raise
    :class:`CorpusError` naming the offending file; nothing is silently skipped
    except dot-files such as ``.gitkeep``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .chunker import chunk_constraints, chunk_task, chunk_wiki
from .store import Chunk

__all__ = ["CorpusError", "load_chunks"]

_WIKI_SUFFIXES = (".md",)
_TASK_SUFFIXES = (".json",)
_CONSTRAINT_SUFFIXES = (".yaml", ".yml")


class CorpusError(RuntimeError):
    """Raised when a corpus file cannot be parsed or yields duplicate ids."""


def _iter_files(directory: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in suffixes and not path.name.startswith(".")
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:  # pragma: no cover - filesystem dependent
        raise CorpusError(f"读取失败：{path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise CorpusError(f"文件不是合法 UTF-8：{path}: {exc}") from exc


def _load_wiki(root: Path) -> list[tuple[Path, list[Chunk]]]:
    results: list[tuple[Path, list[Chunk]]] = []
    for path in _iter_files(root / "wiki", _WIKI_SUFFIXES):
        results.append(
            (
                path,
                chunk_wiki(
                    _read_text(path),
                    source_name=path.stem,
                    source_path=f"wiki/{path.name}",
                ),
            )
        )
    return results


def _load_tasks(root: Path) -> list[tuple[Path, list[Chunk]]]:
    results: list[tuple[Path, list[Chunk]]] = []
    for path in _iter_files(root / "tasks", _TASK_SUFFIXES):
        try:
            payload = json.loads(_read_text(path))
        except json.JSONDecodeError as exc:
            raise CorpusError(f"JSON 解析失败：{path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CorpusError(f"任务文件顶层必须是对象：{path}")
        results.append(
            (
                path,
                chunk_task(payload, source_path=f"tasks/{path.name}"),
            )
        )
    return results


def _load_constraints(root: Path) -> list[tuple[Path, list[Chunk]]]:
    results: list[tuple[Path, list[Chunk]]] = []
    for path in _iter_files(root / "constraints", _CONSTRAINT_SUFFIXES):
        try:
            payload: Any = yaml.safe_load(_read_text(path))
        except yaml.YAMLError as exc:
            raise CorpusError(f"YAML 解析失败：{path}: {exc}") from exc
        if payload is None:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("constraints"), list):
            items = list(payload["constraints"])
        elif isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = list(payload)
        else:
            raise CorpusError(f"约束文件必须是列表或对象：{path}")
        results.append(
            (
                path,
                chunk_constraints(items, source_file=f"constraints/{path.name}"),
            )
        )
    return results


def load_chunks(source_dir: Path | str = "data") -> list[Chunk]:
    """Load and chunk the whole corpus under ``source_dir``."""

    root = Path(source_dir)
    if not root.is_dir():
        raise CorpusError(f"语料目录不存在：{root}")

    batches = [*_load_wiki(root), *_load_tasks(root), *_load_constraints(root)]
    if not batches:
        raise CorpusError(f"语料目录为空（没有 wiki/tasks/constraints 文件）：{root}")

    chunks: list[Chunk] = []
    origin: dict[str, Path] = {}
    for path, batch in batches:
        for chunk in batch:
            previous = origin.get(chunk.chunk_id)
            if previous is not None:
                raise CorpusError(
                    f"chunk_id 重复：{chunk.chunk_id}（{previous} 与 {path}）"
                )
            origin[chunk.chunk_id] = path
            chunks.append(chunk)
    return chunks
