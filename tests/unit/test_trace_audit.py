"""Unit tests for the S7-05 trace completeness audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gameconfig_copilot.evaluation import trace_audit as audit

LLM_ATTRS = {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "cost_cny": 0.002,
}
TOOL_ATTRS = {"args_digest": "sha256:abc", "ok": True}
COMMIT_ATTRS = {"commit_id": "cm_01", "backup_path": "backups/T_SIDE_0214.bak"}


def _span(
    name: str,
    attrs: dict[str, Any],
    *,
    trace_id: str = "tr_1",
    line: int = 0,
) -> dict[str, Any]:
    del line
    return {
        "ts": "2026-11-15T12:00:00+08:00",
        "trace_id": trace_id,
        "span_id": f"sp_{name}",
        "parent_id": None,
        "name": name,
        "kind": "span",
        "duration_ms": 1,
        "status": "ok",
        "attrs": attrs,
    }


def test_valid_llm_tool_commit_spans_pass() -> None:
    report = audit.audit_records(
        [
            _span("llm.chat", LLM_ATTRS),
            _span("tool.search_wiki", TOOL_ATTRS),
            _span("commit", COMMIT_ATTRS),
        ]
    )

    assert report.ok
    assert report.exit_code == 0
    assert report.findings == ()
    assert report.span_counts == {"llm": 1, "tool": 1, "commit": 1}
    assert report.missing_by_owner == {}


def test_llm_missing_attributes_are_grouped_by_owner_f() -> None:
    report = audit.audit_records([_span("llm.chat", {})])

    assert not report.ok
    assert [finding.field for finding in report.findings] == [
        "provider",
        "model",
        "tokens",
        "cost",
    ]
    assert report.missing_by_owner == {"F": 4}
    assert all(finding.owner == "F" for finding in report.findings)
    assert all(finding.line == 1 for finding in report.findings)
    assert report.span_names == {"llm.chat": 1}


def test_llm_tokens_require_prompt_and_completion() -> None:
    attrs = dict(LLM_ATTRS)
    attrs.pop("completion_tokens")

    report = audit.audit_records([_span("llm.chat", attrs)])

    assert [finding.field for finding in report.findings] == ["tokens"]


@pytest.mark.parametrize(
    "tokens",
    [100, {"total": 100}, {"prompt": 10, "completion": 5}],
)
def test_llm_tokens_accept_int_or_object(tokens: Any) -> None:
    attrs = dict(LLM_ATTRS)
    attrs.pop("prompt_tokens")
    attrs.pop("completion_tokens")
    attrs["tokens"] = tokens

    report = audit.audit_records([_span("llm.chat", attrs)])

    assert report.ok


def test_llm_cost_alias_is_accepted() -> None:
    attrs = dict(LLM_ATTRS)
    attrs.pop("cost_cny")
    attrs["cost"] = 0.001

    report = audit.audit_records([_span("llm.chat", attrs)])

    assert report.ok


def test_tool_missing_fields_are_owned_by_a() -> None:
    report = audit.audit_records([_span("tool.preview_draft", {})])

    assert [finding.field for finding in report.findings] == ["args_digest", "ok"]
    assert report.missing_by_owner == {"A": 2}


def test_tool_ok_must_be_a_bool() -> None:
    attrs = dict(TOOL_ATTRS)
    attrs["ok"] = 1

    report = audit.audit_records([_span("tool.search", attrs)])

    assert [finding.field for finding in report.findings] == ["ok"]


@pytest.mark.parametrize("name", ["commit", "agent.commit", "commit.write"])
def test_commit_span_name_variants_are_audited(name: str) -> None:
    report = audit.audit_records([_span(name, {})])

    assert [finding.field for finding in report.findings] == ["commit_id", "backup"]
    assert report.missing_by_owner == {"M": 2}


def test_commit_backup_alias_is_accepted() -> None:
    attrs = {"commit_id": "cm_02", "backup": "backups/x.bak"}

    report = audit.audit_records([_span("agent.commit", attrs)])

    assert report.ok


def test_non_audited_spans_are_ignored() -> None:
    report = audit.audit_records(
        [
            _span("route", {}),
            _span("retrieval", {}),
            _span("api.messages", {}),
        ]
    )

    assert report.ok
    assert report.span_counts == {}
    assert len(report.notes) == 3


def test_malformed_records_are_reported_not_crashed() -> None:
    report = audit.audit_records(
        [
            _span("llm.chat", LLM_ATTRS),
            "not-an-object",  # type: ignore[list-item]
            {"trace_id": "tr_2", "attrs": {}},
        ]
    )

    assert not report.ok
    assert report.exit_code == 1
    assert [item.line for item in report.malformed] == [2, 3]
    assert "span name" in report.malformed[1].reason


def test_audit_traces_reads_jsonl_and_keeps_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    lines = [
        json.dumps(_span("llm.chat", LLM_ATTRS), ensure_ascii=False),
        "not-json",
        json.dumps(_span("tool.search", {"ok": True}), ensure_ascii=False),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = audit.audit_traces(path)

    assert report.total_lines == 3
    assert report.parsed == 2
    assert [finding.line for finding in report.findings] == [3]
    assert [finding.field for finding in report.findings] == ["args_digest"]
    assert [item.line for item in report.malformed] == [2]


def test_audit_traces_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(audit.TraceAuditError, match="不存在"):
        audit.audit_traces(tmp_path / "missing.jsonl")


def test_report_as_dict_lists_rules_and_groups() -> None:
    report = audit.audit_records([_span("llm.chat", {})])
    payload = report.as_dict()

    assert payload["suite"] == "trace_audit"
    assert payload["status"] == "failed"
    assert payload["missing_count"] == 4
    assert payload["missing_by_field"] == {
        "llm.cost": 1,
        "llm.model": 1,
        "llm.provider": 1,
        "llm.tokens": 1,
    }
    assert payload["rules"]["llm"] == ["provider", "model", "tokens", "cost"]


def test_format_report_mentions_owner_and_hint() -> None:
    report = audit.audit_records([_span("tool.search", {})])
    text = audit.format_report(report)

    assert "A · Agent 编排层" in text
    assert "tool.args_digest" in text
    assert "第 1 行" in text


def test_main_returns_exit_code_and_writes_json(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(_span("llm.chat", {})), encoding="utf-8")
    json_path = tmp_path / "audit.json"

    code = audit.main(["--path", str(path), "--json", str(json_path)])

    assert code == 1
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"


def test_main_returns_two_for_missing_file(tmp_path: Path) -> None:
    assert audit.main(["--path", str(tmp_path / "missing.jsonl")]) == 2


def test_empty_trace_file_is_clean() -> None:
    report = audit.audit_records([])

    assert report.ok
    assert report.total_lines == 0
