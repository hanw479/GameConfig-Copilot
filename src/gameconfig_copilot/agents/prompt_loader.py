"""Prompt loading for the agent layer.

Prompts are owned by Worker A (docs/06 §5.2) and live in the repository-level
``prompts/`` directory so they are reviewed as text, not buried in Python
strings. Resolution order:

1. ``GCC_PROMPTS_DIR`` (deployment override),
2. ``<cwd>/prompts``,
3. ``<repo>/prompts`` (``src/gameconfig_copilot/agents/prompt_loader.py`` -> 4 parents),
4. ``src/gameconfig_copilot/agents/prompts`` (package fallback).

Input
    A prompt stem such as ``"query"``.
Output
    The UTF-8 file content.
Failure semantics
    Unknown/unsafe names and missing files raise :class:`PromptNotFoundError`
    listing every directory that was tried, so a deployment typo fails loudly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "PROMPT_SUFFIX",
    "PromptNotFoundError",
    "available_prompts",
    "candidate_dirs",
    "load_prompt",
]

PROMPT_SUFFIX = ".md"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PromptNotFoundError(FileNotFoundError):
    """Raised when a prompt file is missing or its name is unsafe."""


def candidate_dirs() -> list[Path]:
    """Return prompt directories in resolution order (deduplicated)."""

    package_root = Path(__file__).resolve().parent
    repo_root = package_root.parents[2]
    directories = [
        Path(os.environ["GCC_PROMPTS_DIR"]).expanduser()
        if os.environ.get("GCC_PROMPTS_DIR")
        else None,
        Path.cwd() / "prompts",
        repo_root / "prompts",
        package_root / "prompts",
    ]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for directory in directories:
        if directory is None:
            continue
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _resolve(name: str) -> str:
    stem = name.strip()
    if stem.endswith(PROMPT_SUFFIX):
        stem = stem[: -len(PROMPT_SUFFIX)]
    if not _NAME_RE.fullmatch(stem):
        raise PromptNotFoundError(f"非法 prompt 名称：{name!r}（只允许小写字母/数字/下划线）")
    return stem


def load_prompt(name: str) -> str:
    """Read one prompt file and return its text."""

    stem = _resolve(name)
    tried: list[Path] = []
    for directory in candidate_dirs():
        path = directory / f"{stem}{PROMPT_SUFFIX}"
        tried.append(path)
        if not path.is_file():
            continue
        try:
            return path.read_text(encoding="utf-8-sig")
        except OSError as exc:  # pragma: no cover - filesystem dependent
            raise PromptNotFoundError(f"prompt 读取失败：{path}: {exc}") from exc
    locations = "、".join(str(path) for path in tried)
    raise PromptNotFoundError(f"找不到 prompt {stem!r}；已尝试：{locations}")


def available_prompts() -> list[str]:
    """List prompt stems visible in the resolution path."""

    found: set[str] = set()
    for directory in candidate_dirs():
        if not directory.is_dir():
            continue
        for path in directory.glob(f"*{PROMPT_SUFFIX}"):
            if _NAME_RE.fullmatch(path.stem):
                found.add(path.stem)
    return sorted(found)
