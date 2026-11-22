"""Architecture-boundary guards for the G0 gate."""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "gameconfig_copilot"

VENDOR_SDKS = {
    "openai",
    "deepseek",
    "anthropic",
    "google",
    "cohere",
    "dashscope",
    "zhipuai",
    "ollama",
    "litellm",
}


def _python_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_vendor_sdks_and_http_clients_stay_in_providers() -> None:
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(PACKAGE_ROOT)
        if "providers" in relative.parts:
            continue
        imports = _import_roots(path)
        forbidden = (imports & VENDOR_SDKS) | ({"httpx"} if "httpx" in imports else set())
        if forbidden:
            offenders.append(f"{relative}: {sorted(forbidden)}")

    assert offenders == [], f"厂商 SDK / httpx 只能出现在 llm/providers/: {offenders}"


def test_no_hardcoded_absolute_paths_in_source() -> None:
    patterns = (
        re.compile(r"[A-Za-z]:\\\\"),
        re.compile(r"/Users/"),
        re.compile(r"/home/"),
    )
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)} matches {pattern.pattern}")
    assert offenders == []


def test_no_api_keys_committed_in_source() -> None:
    secret_pattern = re.compile(r"sk-[A-Za-z0-9]{16,}")
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _python_files()
        if secret_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_gitignore_excludes_env_and_traces() -> None:
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in lines
    assert ".env.*" in lines
    assert "traces/" in lines


def test_pytyped_marker_exists() -> None:
    assert (PACKAGE_ROOT / "py.typed").is_file()
