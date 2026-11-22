"""Trace completeness audit (S7-05): one JSONL scan, per-owner missing list.

Contract
--------
Input
    ``traces/traces.jsonl`` (or any JSONL in the ``observability/events.py``
    shape: ``ts`` / ``trace_id`` / ``span_id`` / ``name`` / ``kind`` /
    ``duration_ms`` / ``status`` / ``attrs``).
Output
    :class:`AuditReport` — every missing required attribute, grouped by owner
    (F / A / M) and by field, plus coverage counts and malformed lines.
Failure semantics
    * an unreadable path -> :class:`TraceAuditError`;
    * a malformed JSONL line is reported, never silently skipped (it is still a
      failed audit because the trace is incomplete);
    * a missing attribute is reported with line number + span name; this module
      never edits another owner's writer.

Required attributes (docs/02 §9.2)
----------------------------------
``llm.*``  : ``provider``, ``model``, tokens (``prompt_tokens`` + ``completion_tokens``,
             or a ``tokens`` int/object), ``cost_cny`` (alias ``cost``).
``tool.*`` : ``args_digest`` (non-empty string), ``ok`` (bool).
``commit`` : ``commit_id`` (non-empty string), ``backup``/``backup_path``.

The report is the hand-off artifact: each finding names the owner responsible for
that span family, so gaps are fixed by the layer that emits the span.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TRACE_PATH",
    "FIELD_RULES",
    "OWNER_LABELS",
    "AuditReport",
    "FieldRule",
    "MalformedLine",
    "MissingField",
    "TraceAuditError",
    "audit_records",
    "audit_traces",
    "format_report",
    "main",
    "span_family",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TRACE_PATH = REPO_ROOT / "traces" / "traces.jsonl"

OWNER_LABELS: dict[str, str] = {
    "F": "F · 地基 / LLM 适配层",
    "A": "A · Agent 编排层",
    "M": "M · 写入 / MCP 层",
}


class TraceAuditError(RuntimeError):
    """Raised when the trace file cannot be read at all."""


@dataclass(frozen=True)
class FieldRule:
    """One required attribute and the owner responsible for emitting it."""

    family: str
    field: str
    owner: str
    hint: str


FIELD_RULES: tuple[FieldRule, ...] = (
    FieldRule(
        "llm",
        "provider",
        "F",
        "llm.* span 的 attrs.provider 写 provider 名（如 deepseek / openai_compatible）",
    ),
    FieldRule(
        "llm",
        "model",
        "F",
        "llm.* span 的 attrs.model 写实际调用的模型名（与 judge/生成配置一致）",
    ),
    FieldRule(
        "llm",
        "tokens",
        "F",
        "llm.* span 需 attrs.prompt_tokens + completion_tokens（或 tokens 总数字段）",
    ),
    FieldRule(
        "llm",
        "cost",
        "F",
        "llm.* span 需 attrs.cost_cny（别名 cost），由 llm/base.py 统一计算",
    ),
    FieldRule(
        "tool",
        "args_digest",
        "A",
        "tool.* span 需 attrs.args_digest（入参摘要，不放全文）",
    ),
    FieldRule(
        "tool",
        "ok",
        "A",
        "tool.* span 需 attrs.ok（bool，成功/失败）",
    ),
    FieldRule(
        "commit",
        "commit_id",
        "M",
        "commit span 需 attrs.commit_id（与审计记录一致）",
    ),
    FieldRule(
        "commit",
        "backup",
        "M",
        "commit span 需 attrs.backup 或 backup_path（备份文件路径）",
    ),
)

_RULE_BY_KEY: dict[tuple[str, str], FieldRule] = {
    (rule.family, rule.field): rule for rule in FIELD_RULES
}


def span_family(name: str) -> str | None:
    """Classify a span name into the audited families; ``None`` means not audited."""

    if name.startswith("llm."):
        return "llm"
    if name.startswith("tool."):
        return "tool"
    if name == "commit" or name.startswith("commit.") or name.endswith(".commit"):
        return "commit"
    return None


@dataclass(frozen=True)
class MissingField:
    """One missing required attribute on one span."""

    line: int
    trace_id: str | None
    span: str
    family: str
    field: str
    owner: str
    hint: str
    ts: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "ts": self.ts,
            "trace_id": self.trace_id,
            "span": self.span,
            "family": self.family,
            "field": self.field,
            "owner": self.owner,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class MalformedLine:
    """A line that is not a JSON object (or lacks a span name)."""

    line: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "reason": self.reason}


@dataclass(frozen=True)
class AuditReport:
    """Immutable audit result; ``exit_code`` is 0 only when nothing is missing."""

    source: str
    total_lines: int
    parsed: int
    findings: tuple[MissingField, ...]
    malformed: tuple[MalformedLine, ...]
    span_counts: dict[str, int]
    span_names: dict[str, int]
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings and not self.malformed

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    @property
    def missing_by_owner(self) -> dict[str, int]:
        return dict(sorted(Counter(item.owner for item in self.findings).items()))

    @property
    def missing_by_field(self) -> dict[str, int]:
        keys = Counter(f"{item.family}.{item.field}" for item in self.findings)
        return dict(sorted(keys.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": "trace_audit",
            "status": "ok" if self.ok else "failed",
            "source": self.source,
            "total_lines": self.total_lines,
            "parsed": self.parsed,
            "span_counts": dict(sorted(self.span_counts.items())),
            "span_names": dict(sorted(self.span_names.items())),
            "missing_count": len(self.findings),
            "missing": [item.as_dict() for item in self.findings],
            "missing_by_owner": self.missing_by_owner,
            "missing_by_field": self.missing_by_field,
            "malformed": [item.as_dict() for item in self.malformed],
            "notes": list(self.notes),
            "rules": {
                family: [
                    rule.field for rule in FIELD_RULES if rule.family == family
                ]
                for family in ("llm", "tool", "commit")
            },
        }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _has_tokens(attrs: Mapping[str, Any]) -> bool:
    prompt = attrs.get("prompt_tokens")
    completion = attrs.get("completion_tokens")
    if _count(prompt) and _count(completion):
        return True
    tokens = attrs.get("tokens")
    if _count(tokens):
        return True
    if isinstance(tokens, Mapping):
        if _count(tokens.get("total")):
            return True
        return _count(tokens.get("prompt")) and _count(tokens.get("completion"))
    return False


def _has_cost(attrs: Mapping[str, Any]) -> bool:
    value = attrs.get("cost_cny", attrs.get("cost"))
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def _has_backup(attrs: Mapping[str, Any]) -> bool:
    value = attrs.get("backup", attrs.get("backup_path"))
    if isinstance(value, Mapping):
        return bool(value)
    return _text(value)


def _missing_fields(family: str, attrs: Mapping[str, Any]) -> tuple[str, ...]:
    missing: list[str] = []
    if family == "llm":
        if not _text(attrs.get("provider")):
            missing.append("provider")
        if not _text(attrs.get("model")):
            missing.append("model")
        if not _has_tokens(attrs):
            missing.append("tokens")
        if not _has_cost(attrs):
            missing.append("cost")
    elif family == "tool":
        if not _text(attrs.get("args_digest")):
            missing.append("args_digest")
        if not isinstance(attrs.get("ok"), bool):
            missing.append("ok")
    elif family == "commit":
        if not _text(attrs.get("commit_id")):
            missing.append("commit_id")
        if not _has_backup(attrs):
            missing.append("backup")
    else:  # pragma: no cover - callers only pass audited families
        raise TraceAuditError(f"未知 span family：{family}")
    return tuple(missing)


def _build_report(
    parsed: Sequence[tuple[int, Mapping[str, Any]]],
    *,
    total_lines: int,
    malformed: Sequence[MalformedLine],
    source: str,
) -> AuditReport:
    span_counts: Counter[str] = Counter()
    span_names: Counter[str] = Counter()
    findings: list[MissingField] = []
    extra_malformed = list(malformed)
    for line, record in parsed:
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            extra_malformed.append(MalformedLine(line, "缺少非空 span name"))
            continue
        family = span_family(name)
        if family is None:
            continue
        span_counts[family] += 1
        span_names[name] += 1
        attrs = record.get("attrs", {})
        if not isinstance(attrs, Mapping):
            attrs = {}
        for field in _missing_fields(family, attrs):
            rule = _RULE_BY_KEY[(family, field)]
            findings.append(
                MissingField(
                    line=line,
                    trace_id=(
                        record.get("trace_id")
                        if isinstance(record.get("trace_id"), str)
                        else None
                    ),
                    span=name,
                    family=family,
                    field=field,
                    owner=rule.owner,
                    hint=rule.hint,
                    ts=record.get("ts") if isinstance(record.get("ts"), str) else None,
                )
            )

    notes: list[str] = []
    for family in ("llm", "tool", "commit"):
        if span_counts.get(family, 0):
            continue
        owner = next(rule.owner for rule in FIELD_RULES if rule.family == family)
        notes.append(
            f"未发现 {family}.* span：{family} 埋点尚未补全（Owner {owner}），"
            "本次审计无法验证该族属性。"
        )
    return AuditReport(
        source=source,
        total_lines=total_lines,
        parsed=len(parsed),
        findings=tuple(findings),
        malformed=tuple(sorted(extra_malformed, key=lambda item: item.line)),
        span_counts=dict(span_counts),
        span_names=dict(span_names),
        notes=tuple(notes),
    )


def audit_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source: str = "<records>",
) -> AuditReport:
    """Audit already-parsed records (tests / replay); line = 1-based position."""

    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TraceAuditError("records 必须是 JSON 对象数组")
    parsed: list[tuple[int, Mapping[str, Any]]] = []
    malformed: list[MalformedLine] = []
    for line, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            malformed.append(MalformedLine(line, "记录不是 JSON 对象"))
            continue
        parsed.append((line, record))
    return _build_report(
        parsed,
        total_lines=len(records),
        malformed=malformed,
        source=source,
    )


def audit_traces(path: str | Path | None = None) -> AuditReport:
    """Read a JSONL trace file and audit every ``llm.*``/``tool.*``/``commit`` span."""

    target = Path(path) if path is not None else DEFAULT_TRACE_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TraceAuditError(f"轨迹文件不存在：{target}") from exc
    except OSError as exc:
        raise TraceAuditError(f"轨迹文件无法读取：{target}（{exc}）") from exc

    parsed: list[tuple[int, Mapping[str, Any]]] = []
    malformed: list[MalformedLine] = []
    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            malformed.append(MalformedLine(line_number, f"JSON 解析失败：{exc.msg}"))
            continue
        if not isinstance(raw, Mapping):
            malformed.append(MalformedLine(line_number, "记录不是 JSON 对象"))
            continue
        parsed.append((line_number, raw))
    return _build_report(
        parsed,
        total_lines=len(lines),
        malformed=malformed,
        source=str(target),
    )


def format_report(report: AuditReport) -> str:
    """Human-readable per-owner missing list (the hand-off artifact)."""

    coverage = " / ".join(
        f"{family} {report.span_counts.get(family, 0)}"
        for family in ("llm", "tool", "commit")
    )
    lines = [
        f"轨迹完整性审计 · {report.source}",
        f"总行数 {report.total_lines} · 可解析 {report.parsed} · span（{coverage}）· "
        f"缺失 {len(report.findings)} 项 · 解析失败 {len(report.malformed)} 行",
    ]
    if report.ok:
        lines.append("✅ 已出现的 llm.* / tool.* / commit span 均含必需属性。")
    else:
        by_owner: dict[str, list[MissingField]] = {}
        for finding in report.findings:
            by_owner.setdefault(finding.owner, []).append(finding)
        for owner in sorted(by_owner):
            lines.append("")
            lines.append(f"[{OWNER_LABELS.get(owner, owner)}]")
            for finding in by_owner[owner]:
                lines.append(
                    f"  - {finding.family}.{finding.field} @ 第 {finding.line} 行"
                    f" · {finding.span} · trace={finding.trace_id or '?'}"
                    f"\n    → {finding.hint}"
                )
        if report.malformed:
            lines.append("")
            lines.append("[解析失败]")
            for item in report.malformed:
                lines.append(f"  - 第 {item.line} 行：{item.reason}")
    for note in report.notes:
        lines.append(f"注：{note}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: print the missing list and return 0 (clean) / 1 (gaps)."""

    parser = argparse.ArgumentParser(description="审计 JSONL 轨迹的 span 属性完整性")
    parser.add_argument("--path", default=str(DEFAULT_TRACE_PATH), help="轨迹 JSONL 路径")
    parser.add_argument("--json", dest="json_path", default=None, help="另存 JSON 审计报告")
    args = parser.parse_args(argv)
    try:
        report = audit_traces(args.path)
    except TraceAuditError as exc:
        print(f"审计失败：{exc}")
        return 2
    print(format_report(report))
    if args.json_path:
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"JSON 审计报告：{target}")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI glue
    raise SystemExit(main())
