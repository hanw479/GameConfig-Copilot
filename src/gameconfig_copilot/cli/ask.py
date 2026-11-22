"""``gcc ask`` placeholder (S2)."""

from __future__ import annotations

import typer

from ._common import pending


def ask(
    question: str = typer.Argument("", help="自然语言问题。"),
    cited: bool = typer.Option(True, "--cited/--no-cited", help="是否要求带引用回答。"),
) -> None:
    """带引用问答（S2 交付）。"""

    del question, cited
    pending(
        "ask",
        "S2 QueryAgent 与只读闭环（S2-02 / S2-03）",
        "QueryAgent、引用约束与只读工具集尚未实现；不会编造知识库外答案。",
    )
