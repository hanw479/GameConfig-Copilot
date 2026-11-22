"""MCP tool implementations for task configs (S5-02 / S5-03 / S5-05 / S5-09).

Owner: Write Worker M (``docs/06-multi-agent-collaboration.md`` §5.2). Signatures
follow ``docs/02`` §6.2 and payloads follow §6.3. This module composes
:class:`~gameconfig_copilot.mcp_server.writer.Writer`; it never imports
``agents``/``api``.

Transport contract
------------------
Every public method returns a JSON-ready dict and never raises. A refusal comes
back as the documented error envelope::

    {"ok": false, "error": {"code": "E_STALE_REVISION", "message": "...", "details": {...}}}

``ok`` means "the tool call completed". A commit that the forced re-validation
rejects is a normal :class:`CommitResult` (``ok=True``, ``status="rejected"``,
``report``); a stale revision or fingerprint mismatch is a refusal (``ok=False``)
so the Agent must re-preview instead of overwriting someone else's edit.

Tool list
---------
``get_task_config``   TaskConfigView
``list_recent_tasks`` [TaskSummary]
``list_constraints``  [Constraint]
``validate_draft``    ``{report}``
``preview_draft``     PreviewResult
``commit_draft``      CommitResult (write mode only)
``rollback_commit``   CommitResult (write mode only)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gameconfig_copilot.schemas import Constraint

from ..observability.tracer import Tracer, get_tracer
from .writer import E_INVALID_ARGUMENT, Writer, WriterError

__all__ = ["ToolService"]

LOGGER = logging.getLogger(__name__)
E_INTERNAL = "E_INTERNAL"


class ToolService:
    """Bound tool implementations for one :class:`Writer` (one repo root)."""

    def __init__(self, writer: Writer, *, tracer: Tracer | None = None) -> None:
        self.writer = writer
        self._tracer = tracer

    @property
    def tracer(self) -> Tracer:
        return self._tracer or get_tracer()

    # -- read tools ----------------------------------------------------------

    def get_task_config(self, task_id: str) -> dict[str, Any]:
        """Read one committed task config: {task_id, path, revision, content, updated_at}."""

        return self._run(self.writer.read_task, task_id)

    def list_recent_tasks(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """List recently modified task files (newest first).

        Supported ``filters`` keys: ``task_type``, ``tag``, ``level_min``,
        ``level_max``, ``keyword``. ``limit`` is clamped to 1..100.
        """

        return self._run(self.writer.list_tasks, filters, limit)

    def list_constraints(self, scope: str | None = None) -> dict[str, Any]:
        """List the frozen YAML constraints, optionally by constraint id or scope prefix."""

        return self._run(self._list_constraints, scope)

    # -- validation / preview ------------------------------------------------

    def validate_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Run the full deterministic validator on a draft; returns {report}."""

        return self._run(self._validate, draft)

    def preview_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Return target path, base revision, unified diff, stats and fingerprint."""

        return self._run(self.writer.preview, draft)

    # -- write tools (registered only in --mode write) -----------------------

    def commit_draft(
        self,
        draft: dict[str, Any],
        fingerprint: str,
        expected_revision: str,
        author: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Commit a previewed draft after fingerprint + revision + forced re-check."""

        with self.tracer.span(
            "commit",
            attrs={
                "tool": "commit_draft",
                "task_id": _draft_task_id(draft),
                "author": author,
                "fingerprint": fingerprint,
                "revision_before": expected_revision,
            },
        ) as span:
            payload = self._run(
                self.writer.commit,
                draft,
                fingerprint=fingerprint,
                expected_revision=expected_revision,
                author=author,
                note=note,
            )
            commit_id = str(payload.get("commit_id") or "")
            if payload.get("ok") is True and payload.get("status") == "committed" and commit_id:
                span.update_attrs(
                    {
                        "ok": True,
                        "status": "committed",
                        "commit_id": commit_id,
                        "backup": payload.get("backup_path")
                        or {"none": True, "reason": "create"},
                        "backup_path": payload.get("backup_path"),
                        "revision_after": str(payload.get("new_revision") or ""),
                    }
                )
            else:
                # A refused/rejected write is not a commit operation: keep it out
                # of the audited ``commit`` family instead of faking an id.
                span.name = "commit_rejected"
                span.update_attrs(
                    {"ok": False, "error_code": _commit_error_code(payload)}
                )
            return payload

    def rollback_commit(self, commit_id: str) -> dict[str, Any]:
        """Restore the pre-commit backup of ``commit_id`` as a new commit record."""

        return self._run(self.writer.rollback, commit_id)

    # -- internals -----------------------------------------------------------

    def _validate(self, draft: Any) -> dict[str, Any]:
        return {"report": self.writer.validate(draft).to_dict()}

    def _list_constraints(self, scope: str | None) -> dict[str, Any]:
        constraints = self.writer.constraints
        selected = constraints if scope is None else _filter_constraints(constraints, scope)
        return {
            "constraints": [
                constraint.model_dump(mode="json", by_alias=True)
                for constraint in selected
            ],
            "count": len(selected),
        }

    def _run(self, action: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = action(*args, **kwargs)
        except WriterError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001 - tool boundary must not leak stacks
            LOGGER.exception("MCP tool crashed: %s", getattr(action, "__name__", action))
            return {
                "ok": False,
                "error": {
                    "code": E_INTERNAL,
                    "message": f"工具内部异常：{type(exc).__name__}",
                    "details": {"type": type(exc).__name__, "detail": str(exc)},
                },
            }
        if isinstance(payload, Mapping):
            return {"ok": True, **payload}
        return {"ok": True, "result": payload}


def _draft_task_id(draft: Any) -> str:
    if isinstance(draft, Mapping):
        value = draft.get("task_id")
        if isinstance(value, str):
            return value.strip()
    return ""


def _commit_error_code(payload: Mapping[str, Any]) -> str:
    """Error code for a refused/rejected commit span (no fabricated commit id)."""

    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    report = payload.get("report")
    if isinstance(report, Mapping):
        errors = report.get("errors")
        if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
            for item in errors:
                code = (
                    item.get("code")
                    if isinstance(item, Mapping)
                    else getattr(item, "code", None)
                )
                if isinstance(code, str) and code.strip():
                    return code.strip()
    return "E_COMMIT_FAILED"


def _filter_constraints(constraints: list[Constraint], scope: str) -> list[Constraint]:
    if not isinstance(scope, str) or not scope.strip():
        raise WriterError(
            E_INVALID_ARGUMENT,
            "scope 必须是非空字符串（或省略）",
            {"scope": repr(scope)},
        )
    needle = scope.strip()
    return [
        constraint
        for constraint in constraints
        if constraint.id == needle
        or constraint.scope == needle
        or constraint.scope.startswith(f"{needle}.")
        or constraint.scope.startswith(f"{needle}[")
        or constraint.target == needle
    ]
