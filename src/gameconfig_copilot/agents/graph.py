"""Agent graph: route -> (query | task | other), with contract-compliant nodes.

**LangGraph substitution (ADR, Wave 2).** ``langgraph`` is not installable in
this sandbox (`pip install langgraph` is denied write access to site-packages),
so this module implements a minimal state machine instead of importing
LangGraph. Node names, the conditional edge topology and the ``AgentState``
fields are exactly those frozen in docs/02 §4; the graph API (``add_node``/
``add_edge``/``add_conditional_edges``/``stream``/``invoke``,
``recursion_limit``) mirrors LangGraph's so Wave 3 can port the graph by
replacing the engine only. No LangGraph symbol is faked or monkey-patched.

Proposed ADR (not appended to docs/02 §11 because ``docs/`` is read-only for
Worker A; Coordinator C must ratify it): "For Wave 2, replace LangGraph with an
internal 9-node state machine because the dependency cannot be installed; keep
docs/02 §4 node/state contracts and a LangGraph-shaped engine API so the
migration is a one-file change."

Topology::

    route --(query)--> research --> compose -> END
          --(task)---> gather_context --> draft --> validate
                                      validate --(ok)--> preview
                                                --(bad, round<2)--> repair --> validate
                                                --(bad, round>=2)--> END (rejected)
                                      preview --(ok)--> human_gate
                                      human_gate --(confirmed)--> commit -> END
                                                 --(awaiting/rejected/cancelled)--> END
          --(other)--> clarify -> END

Node contracts follow docs/02 §4.2: route outputs intent (failure -> clarify);
gather_context is fail-closed on constraint/tool errors; draft retries schemas
once then rejects; validate never trusts the model; repair always carries the
previous report's ``severity="error"`` items plus the relevant constraint text
and stops at 2 rounds; preview is read-only and rejects illegal paths.

``human_gate`` is split across layers on purpose: the graph node only turns an
injected ``human_decision`` (or its absence) into a stage, while
``api/routes.py`` owns the ``commit_token`` and the HTTP confirm/reject
endpoints (docs/02 §10.2). A run that has not been confirmed ends in
``stage="awaiting_confirmation"`` and performs **no** write. The ``commit`` node
is reached by resuming the same state at ``human_gate`` with
``human_decision={"status": "confirmed"}``; it delegates the write to an
injected committer (the MCP ``commit_draft`` tool in production), which repeats
the fingerprint/revision checks and the full validation under the file lock.

Input
    ``AgentState`` from :func:`new_state`.
Output
    ``GraphStep`` stream (per-node update + merged state) or the final state.
Failure semantics
    Unexpected node exceptions propagate to the API layer, which emits an
    ``error`` SSE event; expected domain failures are recorded in ``state.error``
    with ``stage="rejected"``. A commit refusal keeps the target file untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..llm import wrap_llm
from ..observability.tracer import Tracer, get_tracer
from ..schemas import ValidationReport
from .context_builder import ContextBuilder
from .preview import PreviewError, build_preview, normalize_preview
from .query_agent import QueryAgent
from .state import AgentState, Evidence, Intent, Preference, new_state
from .supervisor import Supervisor
from .task_agent import TaskAgent, TaskGenerationError
from .tools import ReadOnlyTools
from .validation import fail_closed_report, schema_validate

__all__ = [
    "NODE_NAMES",
    "AgentGraph",
    "GraphEdgeError",
    "GraphNodeError",
    "GraphRecursionError",
    "GraphStep",
    "build_agent_graph",
    "build_graph",
]

NODE_NAMES: tuple[str, ...] = (
    "route",
    "research",
    "compose",
    "gather_context",
    "draft",
    "validate",
    "repair",
    "preview",
    "human_gate",
    "commit",
    "clarify",
)

_NODE_SPAN_NAMES = {
    "route": "route",
    "research": "retrieval",
    "gather_context": "retrieval",
}

_LIST_ACCUMULATE = frozenset({"warnings"})
_NUMBER_ACCUMULATE = frozenset({"tokens", "cost_cny"})


def _commit_span_attrs(
    state: Mapping[str, Any],
    update: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Attributes that make ``agent.commit`` a complete S7-05 commit span.

    ``None`` means the node did not actually commit (refusal/rejection): such a
    span is renamed so it is not misread as a commit operation with missing ids.
    """

    commit = update.get("commit")
    if not isinstance(commit, Mapping):
        return None
    commit_id = str(commit.get("commit_id") or "")
    if not commit_id:
        return None
    preview = state.get("preview")
    preview = preview if isinstance(preview, Mapping) else {}
    revision_before = preview.get("base_revision") or preview.get("revision")
    return {
        "ok": True,
        "commit_id": commit_id,
        # A create has no pre-existing file, hence no backup file; record that
        # explicitly (a non-empty mapping satisfies the audit's backup rule).
        "backup": commit.get("backup") or {"none": True, "reason": "create"},
        "revision_before": str(revision_before or ""),
        "revision_after": str(commit.get("revision") or ""),
    }


class GraphError(RuntimeError):
    """Base class for graph-engine failures."""


class GraphNodeError(GraphError):
    """A node returned a non-dict update or is not registered."""


class GraphEdgeError(GraphError):
    """A selector pointed at an unknown node."""


class GraphRecursionError(GraphError):
    """The graph exceeded its recursion limit (default 12)."""


@dataclass(frozen=True, slots=True)
class GraphStep:
    """One executed node: its update and the merged state after it."""

    node: str
    update: dict[str, Any]
    state: AgentState


@dataclass
class AgentGraph:
    """Minimal LangGraph-compatible state machine (see module docstring)."""

    supervisor: Supervisor
    query_agent: QueryAgent
    task_agent: TaskAgent
    tools: ReadOnlyTools
    context_builder: ContextBuilder = field(default_factory=ContextBuilder)
    validator: Callable[[Mapping[str, Any]], ValidationReport] | None = None
    previewer: Callable[[Mapping[str, Any]], Any] | None = None
    committer: Callable[..., Mapping[str, Any]] | None = None
    tasks_dir: str = "data/tasks"
    preference: Preference | Mapping[str, Any] | None = None
    tracer: Tracer | None = None
    recursion_limit: int = 12
    entry_point: str = "route"
    _nodes: dict[str, Callable[[AgentState], dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _edges: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _conditional: dict[str, Callable[[AgentState], str]] = field(
        default_factory=dict, init=False, repr=False
    )
    _compiled: bool = field(default=False, init=False, repr=False)
    _preference_value: Preference = field(
        default_factory=Preference, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.recursion_limit < 2:
            raise ValueError("recursion_limit 必须 >= 2")
        if isinstance(self.preference, Preference):
            self._preference_value = self.preference
        elif self.preference is None:
            self._preference_value = Preference()
        elif isinstance(self.preference, Mapping):
            self._preference_value = Preference.from_mapping(self.preference)
        else:
            raise TypeError("preference 必须是 Preference、映射或 None")
        self._validator = self.validator or schema_validate
        self._tracer = self.tracer or get_tracer()
        self._register()

    @property
    def node_names(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    # ------------------------------------------------------------- builder API
    def add_node(self, name: str, func: Callable[[AgentState], dict[str, Any]]) -> None:
        if name in self._nodes:
            raise GraphNodeError(f"节点重复注册：{name}")
        if not callable(func):
            raise GraphNodeError(f"节点 {name} 必须可调用")
        self._nodes[name] = func

    def add_edge(self, source: str, target: str) -> None:
        self._edges[source] = target

    def add_conditional_edges(
        self,
        source: str,
        selector: Callable[[AgentState], str],
    ) -> None:
        if not callable(selector):
            raise GraphEdgeError(f"条件边选择器必须可调用：{source}")
        self._conditional[source] = selector

    def compile(self) -> AgentGraph:  # noqa: A003 - mirrors LangGraph's API
        if self.entry_point not in self._nodes:
            raise GraphEdgeError(f"入口节点未注册：{self.entry_point}")
        for source, target in self._edges.items():
            if source not in self._nodes:
                raise GraphEdgeError(f"边源节点未注册：{source}")
            if target != "END" and target not in self._nodes:
                raise GraphEdgeError(f"边目标节点未注册：{source} -> {target}")
        for source in self._conditional:
            if source not in self._nodes:
                raise GraphEdgeError(f"条件边源节点未注册：{source}")
        self._compiled = True
        return self

    # --------------------------------------------------------------- execution
    def stream(
        self,
        state: AgentState | Mapping[str, Any] | None = None,
        *,
        recursion_limit: int | None = None,
        start: str | None = None,
    ) -> Iterator[GraphStep]:
        """Yield one :class:`GraphStep` per node until ``END``.

        ``start`` resumes a persisted state at a node (used by the confirm
        endpoint to run ``human_gate -> commit`` without re-drafting); it
        defaults to the compiled entry point.
        """

        if not self._compiled:
            self.compile()
        working: AgentState = dict(state or {})  # type: ignore[assignment]
        working["visited"] = []
        limit = recursion_limit or self.recursion_limit
        node = start or self.entry_point
        if node not in self._nodes:
            raise GraphEdgeError(f"起始节点未注册：{node}")
        steps = 0
        while node != "END":
            if steps >= limit:
                raise GraphRecursionError(
                    f"图执行超过 recursion_limit={limit}，可能存在修复死循环"
                )
            func = self._nodes.get(node)
            if func is None:
                raise GraphEdgeError(f"未知节点：{node}")
            span_name = _NODE_SPAN_NAMES.get(node, f"agent.{node}")
            with self._tracer.span(
                span_name,
                attrs={"agent.node": node, "agent.stage": working.get("stage", "")},
            ) as span:
                update = func(working)
                if not isinstance(update, dict):
                    raise GraphNodeError(f"节点 {node} 必须返回 dict 更新")
                span.set_attr("stage", update.get("stage", working.get("stage", "")))
                if node == "commit":
                    commit_attrs = _commit_span_attrs(working, update)
                    if commit_attrs is None:
                        # A refused/rejected attempt is not a commit operation.
                        span.name = "agent.commit_rejected"
                        error = update.get("error")
                        if isinstance(error, Mapping):
                            span.update_attrs(
                                {"ok": False, "error_code": str(error.get("code") or "")}
                            )
                    else:
                        span.update_attrs(commit_attrs)
            self._merge(working, update)
            working["visited"].append(node)
            yield GraphStep(node=node, update=update, state=dict(working))  # type: ignore[arg-type]
            steps += 1
            node = self._next_node(node, working)

    def invoke(
        self,
        state: AgentState | Mapping[str, Any] | None = None,
        *,
        recursion_limit: int | None = None,
        start: str | None = None,
    ) -> AgentState:
        """Run to completion and return the final state."""

        final: AgentState = dict(state or {})  # type: ignore[assignment]
        for step in self.stream(final, recursion_limit=recursion_limit, start=start):
            final = step.state
        return final

    # ---------------------------------------------------------------- topology
    def _register(self) -> None:
        self.add_node("route", self._route_node)
        self.add_node("research", self._research_node)
        self.add_node("compose", self._compose_node)
        self.add_node("gather_context", self._gather_context_node)
        self.add_node("draft", self._draft_node)
        self.add_node("validate", self._validate_node)
        self.add_node("repair", self._repair_node)
        self.add_node("preview", self._preview_node)
        self.add_node("human_gate", self._human_gate_node)
        self.add_node("commit", self._commit_node)
        self.add_node("clarify", self._clarify_node)

        self.add_edge("route", "END")
        self.add_conditional_edges("route", self._after_route)
        self.add_edge("research", "compose")
        self.add_edge("compose", "END")
        self.add_conditional_edges("gather_context", self._after_gather_context)
        self.add_conditional_edges("draft", self._after_draft)
        self.add_conditional_edges("validate", self._after_validate)
        self.add_conditional_edges("repair", self._after_repair)
        self.add_conditional_edges("preview", self._after_preview)
        self.add_conditional_edges("human_gate", self._after_human_gate)
        self.add_edge("commit", "END")
        self.add_edge("clarify", "END")
        self.compile()

    def _next_node(self, node: str, state: AgentState) -> str:
        selector = self._conditional.get(node)
        target = (
            selector(state)
            if selector is not None
            else self._edges.get(node, "END")
        )
        if target != "END" and target not in self._nodes:
            raise GraphEdgeError(f"节点 {node} 指向未注册的目标：{target}")
        return target

    @staticmethod
    def _after_route(state: AgentState) -> str:
        intent = state.get("intent")
        if intent == Intent.QUERY.value:
            return "research"
        if intent == Intent.TASK.value:
            return "gather_context"
        return "clarify"

    @staticmethod
    def _after_gather_context(state: AgentState) -> str:
        return "END" if state.get("stage") == "rejected" else "draft"

    @staticmethod
    def _after_draft(state: AgentState) -> str:
        return "END" if state.get("stage") == "rejected" else "validate"

    @staticmethod
    def _after_validate(state: AgentState) -> str:
        report = state.get("report") or {}
        if report.get("ok"):
            return "preview"
        if int(state.get("repair_round", 0)) >= 2:
            return "END"
        return "repair"

    @staticmethod
    def _after_repair(state: AgentState) -> str:
        return "END" if state.get("stage") == "rejected" else "validate"

    @staticmethod
    def _after_preview(state: AgentState) -> str:
        if state.get("stage") == "rejected" or not state.get("preview"):
            return "END"
        return "human_gate"

    @staticmethod
    def _after_human_gate(state: AgentState) -> str:
        decision = state.get("human_decision") or {}
        if decision.get("status") == "confirmed" and state.get("stage") == "confirmed":
            return "commit"
        return "END"

    @staticmethod
    def _merge(state: AgentState, update: Mapping[str, Any]) -> None:
        for key, value in update.items():
            if key in _LIST_ACCUMULATE and isinstance(value, list):
                existing = state.get(key)
                state[key] = [*existing, *value] if isinstance(existing, list) else value
            elif key in _NUMBER_ACCUMULATE and isinstance(value, int | float):
                state[key] = state.get(key, 0) + value  # type: ignore[literal-required]
            else:
                state[key] = value  # type: ignore[literal-required]

    # ------------------------------------------------------------------- nodes
    def _route_node(self, state: AgentState) -> dict[str, Any]:
        decision = self.supervisor.route(state.get("request", ""))
        return {
            "intent": decision.intent.value,
            "stage": "routing",
            "route_decision": decision.to_dict(),
        }

    def _research_node(self, state: AgentState) -> dict[str, Any]:
        evidence, tools = self.query_agent.collect_evidence(state["request"])
        return {
            "evidence": evidence,
            "stage": "researching",
            "tools": list(tools),
            "tokens": sum(len(item.text) for item in evidence) // 4,
        }

    def _compose_node(self, state: AgentState) -> dict[str, Any]:
        result = self.query_agent.compose(
            state["request"],
            list(state.get("evidence") or []),
            tools_used=tuple(state.get("tools", [])),  # type: ignore[arg-type]
        )
        return {
            "answer": result.answer,
            "citations": list(result.citations),
            "warnings": [dict(item) for item in result.warnings],
            "tokens": result.tokens,
            "stage": "answered",
        }

    def _gather_context_node(self, state: AgentState) -> dict[str, Any]:
        request = state["request"]
        try:
            world = self.tools.search_wiki(request, top_k=4)
            similar = self.tools.find_similar_tasks(request, top_k=3)
            constraints = self.tools.list_constraints(query=request, top_k=8)
        except Exception as exc:  # noqa: BLE001 - fail-closed by contract
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_CONSTRAINT_LOAD_FAILED",
                    "message": f"约束/上下文加载失败，已中止生成（fail-closed）：{exc}",
                    "stage": "rejected",
                },
            }

        evidence: list[Evidence] = []
        seen: set[str] = set()
        for item in [*world, *similar]:
            if item.citation in seen:
                continue
            seen.add(item.citation)
            evidence.append(item)

        built = self.context_builder.build(
            request,
            evidence=evidence,
            constraints=constraints,
            preference=self._preference_value,
        )
        warnings = built.warning_dicts
        if not constraints:
            warnings.append(
                {
                    "code": "NO_CONSTRAINTS",
                    "message": "未检索到生效约束，生成结果可能缺少业务规则依据。",
                    "block": "constraints",
                    "detail": [],
                }
            )
        return {
            "evidence": evidence,
            "constraints": list(constraints),
            "preference": self._preference_value,
            "context": built,
            "warnings": warnings,
            "stage": "gathering_context",
        }

    def _draft_node(self, state: AgentState) -> dict[str, Any]:
        try:
            result = self.task_agent.generate(
                state["request"],
                evidence=list(state.get("evidence") or []),
                constraints=list(state.get("constraints") or []),
                preference=state.get("preference") or self._preference_value,
                context=state.get("context"),
            )
        except TaskGenerationError as exc:
            return {
                "stage": "rejected",
                "error": {**exc.to_dict(), "stage": "rejected"},
            }
        return {
            "draft": result.draft,
            "draft_version": int(state.get("draft_version", 0)) + 1,
            "tokens": result.tokens,
            "stage": "drafting",
        }

    def _validate_node(self, state: AgentState) -> dict[str, Any]:
        draft = state.get("draft") or {}
        try:
            raw_report = self._validator(draft)
            if isinstance(raw_report, ValidationReport):
                report = raw_report
            elif isinstance(raw_report, Mapping):
                report = ValidationReport.model_validate(dict(raw_report))
            else:
                raise TypeError(
                    "validator 必须返回 ValidationReport，"
                    f"收到 {type(raw_report).__name__}"
                )
        except Exception as exc:  # noqa: BLE001 - fail-closed by contract
            report = fail_closed_report(exc)

        report_dict = report.to_dict()
        update: dict[str, Any] = {"report": report_dict, "stage": "validating"}
        if not report.ok and int(state.get("repair_round", 0)) >= 2:
            first = report.errors[0] if report.errors else None
            update["stage"] = "rejected"
            update["error"] = {
                "code": first.code.value if first is not None else "E_VALIDATION_REQUIRED",
                "message": "校验失败且修复轮次已达上限（2 轮），草案被拒绝，未产生任何写操作。",
                "stage": "rejected",
            }
        return update

    def _repair_node(self, state: AgentState) -> dict[str, Any]:
        current_round = int(state.get("repair_round", 0))
        if current_round >= 2:
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_VALIDATION_REQUIRED",
                    "message": "修复轮次已达上限（2 轮），草案被拒绝。",
                    "stage": "rejected",
                },
            }
        report = state.get("report") or {}
        errors = [
            item
            for item in report.get("errors") or ()
            if isinstance(item, Mapping)
            and str(getattr(item.get("severity"), "value", item.get("severity", "error")))
            == "error"
        ]
        if not errors:
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_VALIDATION_REQUIRED",
                    "message": "缺少上一轮 error 级 ValidationReport，禁止盲重试，草案被拒绝。",
                    "stage": "rejected",
                },
            }
        try:
            result = self.task_agent.repair(
                state["request"],
                state.get("draft") or {},
                report,
                evidence=list(state.get("evidence") or []),
                constraints=list(state.get("constraints") or []),
                preference=state.get("preference") or self._preference_value,
                context=state.get("context"),
            )
        except TaskGenerationError as exc:
            return {
                "stage": "rejected",
                "error": {**exc.to_dict(), "stage": "rejected"},
            }
        return {
            "draft": result.draft,
            "draft_version": int(state.get("draft_version", 0)) + 1,
            "repair_round": current_round + 1,
            "tokens": result.tokens,
            "stage": "repairing",
        }

    def _preview_node(self, state: AgentState) -> dict[str, Any]:
        draft = state.get("draft") or {}
        try:
            raw = (
                self.previewer(draft)
                if self.previewer is not None
                else build_preview(draft, tasks_dir=self.tasks_dir)
            )
            preview = normalize_preview(raw)
        except PreviewError as exc:
            return {
                "stage": "rejected",
                "error": {**exc.to_dict(), "stage": "rejected"},
            }
        except Exception as exc:  # noqa: BLE001 - preview must fail closed
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_IO",
                    "message": f"预览失败，已拒绝：{type(exc).__name__}: {exc}",
                    "stage": "rejected",
                },
            }
        return {"preview": preview, "stage": "previewed"}

    def _human_gate_node(self, state: AgentState) -> dict[str, Any]:
        """Turn an injected human decision into a stage; never writes itself."""

        preview = state.get("preview") or {}
        if not preview:
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_VALIDATION_REQUIRED",
                    "message": "没有预览结果，拒绝进入人工确认。",
                    "stage": "rejected",
                },
            }
        decision = state.get("human_decision") or {}
        status = str(decision.get("status") or "awaiting_confirmation")
        if status == "confirmed":
            return {
                "stage": "confirmed",
                "human_decision": {
                    "status": "confirmed",
                    "draft_id": str(decision.get("draft_id") or preview.get("draft_id", "")),
                    "note": str(decision.get("note") or ""),
                },
            }
        if status == "rejected":
            return {
                "stage": "rejected",
                "human_decision": {**decision, "status": "rejected"},
                "error": {
                    "code": "E_HUMAN_REJECTED",
                    "message": f"人工驳回：{decision.get('reason') or '未填写理由'}",
                    "stage": "rejected",
                },
            }
        if status == "cancelled":
            return {
                "stage": "cancelled",
                "error": {
                    "code": "E_CONFIRM_TIMEOUT",
                    "message": "等待人工确认超时，草案已取消（未写入）。",
                    "stage": "cancelled",
                },
            }
        if status not in {"awaiting_confirmation", "pending"}:
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_COMMIT_TOKEN_INVALID",
                    "message": f"未知的人工确认状态：{status!r}",
                    "stage": "rejected",
                },
            }
        return {"stage": "awaiting_confirmation"}

    def _commit_node(self, state: AgentState) -> dict[str, Any]:
        """Call the injected committer (MCP ``commit_draft``); never guess."""

        decision = state.get("human_decision") or {}
        preview = state.get("preview") or {}
        draft = state.get("draft") or {}
        if decision.get("status") != "confirmed":
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_CONFIRMATION_REQUIRED",
                    "message": "没有人工确认，拒绝写入。",
                    "stage": "rejected",
                },
            }
        decision_draft_id = str(decision.get("draft_id") or "")
        preview_draft_id = str(preview.get("draft_id") or "")
        if decision_draft_id and preview_draft_id and decision_draft_id != preview_draft_id:
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_FINGERPRINT_MISMATCH",
                    "message": "确认的草案与预览的草案不一致，拒绝写入。",
                    "stage": "rejected",
                },
            }
        if not preview.get("fingerprint"):
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_FINGERPRINT_MISMATCH",
                    "message": "预览缺少指纹，拒绝写入。",
                    "stage": "rejected",
                },
            }
        if self.committer is None:
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_WRITE_DISABLED",
                    "message": "写模式未启用，commit_draft 不可用，未写入任何文件。",
                    "stage": "rejected",
                },
            }
        expected_revision = str(
            preview.get("base_revision") or preview.get("revision") or ""
        )
        try:
            raw = self.committer(
                draft,
                fingerprint=str(preview["fingerprint"]),
                expected_revision=expected_revision,
                author=str(state.get("designer_id") or "anonymous"),
                note=str(decision.get("note") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary converts to rejected
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_COMMIT_FAILED",
                    "message": f"提交失败，目标文件未写入：{type(exc).__name__}: {exc}",
                    "stage": "rejected",
                },
            }
        if not isinstance(raw, Mapping):
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_COMMIT_FAILED",
                    "message": "commit_draft 返回值不是映射，未写入。",
                    "stage": "rejected",
                },
            }
        envelope = dict(raw)
        if envelope.get("ok") is False:
            error = envelope.get("error")
            details = dict(error) if isinstance(error, Mapping) else {}
            return {
                "stage": "rejected",
                "error": {
                    "code": str(details.get("code") or "E_COMMIT_FAILED"),
                    "message": str(details.get("message") or "提交被拒绝"),
                    "details": dict(details.get("details") or {}),
                    "stage": "rejected",
                },
            }
        status = str(envelope.get("status") or "")
        if status == "rejected":
            report = envelope.get("report") or {}
            errors = list(report.get("errors") or []) if isinstance(report, Mapping) else []
            first = errors[0] if errors else {}
            return {
                "stage": "rejected",
                "error": {
                    "code": str(first.get("code") or "E_VALIDATION_REQUIRED"),
                    "message": "提交前复检未通过，草案被拒绝，未写入任何文件。",
                    "stage": "rejected",
                },
                "report": dict(report) if isinstance(report, Mapping) else {},
            }
        if status != "committed":
            return {
                "stage": "rejected",
                "error": {
                    "code": "E_COMMIT_FAILED",
                    "message": f"commit_draft 返回未知状态：{status!r}",
                    "stage": "rejected",
                },
            }
        commit = {
            "status": "committed",
            "commit_id": str(envelope.get("commit_id") or ""),
            "backup": envelope.get("backup_path"),
            "revision": envelope.get("new_revision"),
            "target_path": str(preview.get("target_path") or ""),
            "report": envelope.get("report") or {},
        }
        return {"stage": "committed", "commit": commit}

    def _clarify_node(self, state: AgentState) -> dict[str, Any]:
        del state
        return {
            "answer": "无法判断意图：请说明是要查询知识库，还是要生成/修改任务配置。",
            "citations": [],
            "stage": "clarify",
        }


def build_agent_graph(
    llm: Any,
    retriever: Any,
    *,
    tracer: Tracer | None = None,
    validator: Callable[[Mapping[str, Any]], ValidationReport] | None = None,
    previewer: Callable[[Mapping[str, Any]], Any] | None = None,
    committer: Callable[..., Mapping[str, Any]] | None = None,
    tasks_dir: str = "data/tasks",
    preference: Preference | Mapping[str, Any] | None = None,
    context_builder: ContextBuilder | None = None,
    existing_ids_provider: Callable[[], Sequence[str]] | None = None,
    routing_llm: Any | None = None,
) -> AgentGraph:
    """Wire the default S2/S3/S4/S5 graph from an ``LLMClient`` and a ``Retriever``.

    ``previewer`` / ``committer`` are the MCP ``preview_draft`` / ``commit_draft``
    callables in production; when omitted the graph previews in-process and the
    commit node refuses with ``E_WRITE_DISABLED`` (read-only default).
    """

    traced_llm = wrap_llm(llm, tracer=tracer)
    traced_routing = wrap_llm(routing_llm, tracer=tracer)
    tools = ReadOnlyTools(retriever, tracer=tracer)
    builder = context_builder or ContextBuilder()
    graph = AgentGraph(
        supervisor=Supervisor(llm=traced_routing),
        query_agent=QueryAgent(retriever, traced_llm, tools=tools),
        task_agent=TaskAgent(
            traced_llm,
            context_builder=builder,
            existing_ids_provider=existing_ids_provider or tools.existing_task_ids,
        ),
        tools=tools,
        context_builder=builder,
        validator=validator,
        previewer=previewer,
        committer=committer,
        tasks_dir=tasks_dir,
        preference=preference,
        tracer=tracer,
    )
    graph.compile()
    return graph


def run_request(
    graph: AgentGraph,
    request: str,
    *,
    session_id: str = "",
    trace_id: str = "",
    designer_id: str = "",
) -> AgentState:
    """Convenience helper: build state, run the graph, return the final state."""

    return graph.invoke(
        new_state(
            request,
            session_id=session_id,
            trace_id=trace_id,
            designer_id=designer_id,
        )
    )


#: Alias kept for callers that expect ``build_graph`` (brief/CLI ergonomics).
build_graph = build_agent_graph
