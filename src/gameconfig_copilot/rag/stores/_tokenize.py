"""Chinese-aware tokenizer shared by the BM25 adapters.

The corpus is Chinese prose with embedded ASCII entity ids such as
``MAP_MINE_03``. Word segmentation is intentionally dependency-free: CJK runs
become overlapping **2-grams** while ASCII/digit runs stay whole tokens. This is
the standard cheap baseline for Chinese BM25 and keeps retrieval reproducible
without jieba.

Input
    Raw chunk text or query text.
Output
    Lower-cased token list, in reading order.
Failure semantics
    Non-string input raises :class:`TypeError`; empty text yields ``[]``.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["is_cjk", "tokenize"]

_ASCII_CHAR_RE = re.compile(r"[a-z0-9_]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def is_cjk(char: str) -> bool:
    return bool(_CJK_RE.match(char))


def _tokens_for_cjk_run(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[index : index + 2] for index in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/ASCII text into 2-grams and ASCII words."""

    if not isinstance(text, str):
        raise TypeError("tokenize 只接受字符串")
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    ascii_buffer: list[str] = []
    cjk_buffer: list[str] = []

    def flush_ascii() -> None:
        if ascii_buffer:
            tokens.append("".join(ascii_buffer))
            ascii_buffer.clear()

    def flush_cjk() -> None:
        if cjk_buffer:
            tokens.extend(_tokens_for_cjk_run("".join(cjk_buffer)))
            cjk_buffer.clear()

    for char in normalized:
        if _ASCII_CHAR_RE.match(char):
            flush_cjk()
            ascii_buffer.append(char)
        elif is_cjk(char):
            flush_ascii()
            cjk_buffer.append(char)
        else:
            flush_ascii()
            flush_cjk()
    flush_ascii()
    flush_cjk()
    return tokens
