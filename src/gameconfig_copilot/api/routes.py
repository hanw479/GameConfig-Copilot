"""REST + SSE routes (docs/02 §10.1/§10.2).

Endpoints::

    POST /api/v1/sessions              create
    GET  /api/v1/sessions/{id}         snapshot
    POST /api/v1/sessions/{id}/resume  resume + TTL refresh
    POST /api/v1/messages              SSE stream (one graph run)
    POST /api/v1/drafts/{id}/confirm   confirm a previewed draft (write mode)
    POST /api/v1/drafts/{id}/reject    reject a previewed draft with a reason
    GET  /api/v1/traces/{trace_id}     JSONL trace slices

SSE ordering is delegated to :class:`gameconfig_copilot.api.sse.SSEStream`,
which enforces ``meta`` first and ``done`` last. Query answers travel on the
final ``status`` event (``stage="answered"``) because §10.2 has no dedicated
answer event; no new event name is introduced.

Write closure (S5-07)
---------------------
``/messages`` stops at ``stage="awaiting_confirmation"`` and emits
``awaiting_confirmation`` with an API-issued ``commit_token``. The token is
opaque and bound server-side to ``draft_id + fingerprint + expires_at``; the
previewed draft/preview/state is kept in the session store. ``/confirm``
re-validates the token (scope, expiry, draft digest, one-shot), resumes the
graph at ``human_gate`` and runs ``commit`` — the MCP ``commit_draft`` tool
re-checks fingerprint/revision and the full rule pipeline under the file lock —
then streams ``committed`` (``commit_id``/``backup``/``revision``) + ``done``.
``/reject`` records the reason and marks the draft ``rejected``.

Failure semantics
    Unknown session -> 404, expired -> 410 (before the stream starts). Unknown
    or expired token -> 404/410; a token used for another draft, re-used, or
    with a mutated draft -> 409. Errors inside a stream produce ``error``
    followed by ``done(status="error")``; the session transcript is always
    updated.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agents.preview import fingerprint_draft
from ..agents.state import AgentState, new_state
from ..observability.events import new_trace_id
from ..observability.tracer import redact_text, trace
from .runtime import AppRuntime
from .sessions import (
    PendingCommit,
    Session,
    SessionExpiredError,
    SessionNotFoundError,
)
from .sse import STAGE_LABELS, SSEEvent, SSEStream

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class CreateSessionRequest(BaseModel):
    designer_id: str | None = Field(default=None, max_length=80)


class SessionView(BaseModel):
    session_id: str
    designer_id: str
    created_at: datetime
    expires_at: datetime
    expires_in: int
    message_count: int
    last_trace_id: str | None = None
    last_stage: str = ""
    resumed: bool = False

    @classmethod
    def from_session(
        cls,
        session: Session,
        now: datetime,
        *,
        resumed: bool = False,
    ) -> SessionView:
        return cls(
            session_id=session.session_id,
            designer_id=session.designer_id,
            created_at=session.created_at,
            expires_at=session.expires_at,
            expires_in=session.expires_in(now),
            message_count=len(session.messages),
            last_trace_id=session.last_trace_id,
            last_stage=session.last_stage,
            resumed=resumed,
        )


class MessageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=4000)


class ConfirmRequest(BaseModel):
    commit_token: str = Field(min_length=8, max_length=200)
    note: str = Field(default="", max_length=500)


class RejectRequest(BaseModel):
    commit_token: str = Field(min_length=8, max_length=200)
    reason: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)


class RejectView(BaseModel):
    draft_id: str
    status: str = "rejected"
    reason: str = ""


class TraceView(BaseModel):
    trace_id: str
    events: list[dict[str, Any]]


def get_runtime(request: Request) -> AppRuntime:
    runtime: AppRuntime = request.app.state.runtime
    return runtime


@router.post(
    "/sessions",
    response_model=SessionView,
    status_code=status.HTTP_201_CREATED,
)
def create_session(
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
    payload: CreateSessionRequest | None = None,
) -> SessionView:
    session = runtime.sessions.create(payload.designer_id if payload else None)
    return SessionView.from_session(session, runtime.sessions.now())


@router.get("/sessions/{session_id}", response_model=SessionView)
def get_session(
    session_id: str,
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
) -> SessionView:
    try:
        session = runtime.sessions.get(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}") from exc
    except SessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=f"会话已超时：{session_id}") from exc
    return SessionView.from_session(session, runtime.sessions.now())


@router.post("/sessions/{session_id}/resume", response_model=SessionView)
def resume_session(
    session_id: str,
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
) -> SessionView:
    try:
        session = runtime.sessions.resume(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}") from exc
    except SessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=f"会话已超时：{session_id}") from exc
    return SessionView.from_session(session, runtime.sessions.now(), resumed=True)


@router.post("/messages")
def send_message(
    payload: MessageRequest,
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
) -> StreamingResponse:
    try:
        session = runtime.sessions.get(payload.session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"会话不存在：{payload.session_id}") from exc
    except SessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=f"会话已超时：{payload.session_id}") from exc

    return StreamingResponse(
        _event_stream(runtime, session, payload.message),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/drafts/{draft_id}/confirm")
def confirm_draft(
    draft_id: str,
    payload: ConfirmRequest,
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
) -> StreamingResponse:
    """Resume ``human_gate -> commit`` for a confirmed token (write mode only)."""

    if not runtime.write_enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "E_WRITE_DISABLED",
                "message": "只读模式未启用 commit_draft，拒绝写入。",
            },
        )
    session, pending = _require_pending(runtime, draft_id, payload.commit_token)
    frames = _commit_frames(runtime, session, pending, note=payload.note)
    return StreamingResponse(
        iter(frames),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/drafts/{draft_id}/reject", response_model=RejectView)
def reject_draft(
    draft_id: str,
    payload: RejectRequest,
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
) -> RejectView:
    """Record the rejection reason; the pending draft can never be committed."""

    session, pending = _require_pending(runtime, draft_id, payload.commit_token)
    reason = payload.reason.strip() or "未填写理由"
    runtime.sessions.decide(pending, status="rejected", reason=reason, note=payload.note)
    session.add_message("assistant", f"草案已驳回：{reason}")
    session.last_stage = "rejected"
    return RejectView(draft_id=draft_id, status="rejected", reason=reason)


@router.get("/traces/{trace_id}", response_model=TraceView)
def get_trace(
    trace_id: str,
    runtime: Annotated[AppRuntime, Depends(get_runtime)],
) -> TraceView:
    events = [
        record
        for record in runtime.tracer.read_events()
        if record.get("trace_id") == trace_id
    ]
    if not events:
        raise HTTPException(status_code=404, detail=f"轨迹不存在：{trace_id}")
    return TraceView(trace_id=trace_id, events=events)


# --------------------------------------------------------------------- stream
def _require_pending(
    runtime: AppRuntime,
    draft_id: str,
    token: str,
) -> tuple[Session, PendingCommit]:
    """Second server-side validation of a ``commit_token`` (S5-07).

    Rejects unknown tokens (404), expired sessions/tokens (410), tokens scoped
    to another draft (409 ``E_COMMIT_TOKEN_SCOPE``), re-used tokens (409) and a
    draft whose digest changed after preview (409 ``E_FINGERPRINT_MISMATCH``).
    """

    found = runtime.sessions.find_pending(token)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "E_COMMIT_TOKEN_UNKNOWN", "message": "commit_token 不存在"},
        )
    session, pending = found
    now = runtime.sessions.now()
    if now >= session.expires_at:
        raise HTTPException(
            status_code=410,
            detail={"code": "E_SESSION_EXPIRED", "message": "会话已超时"},
        )
    if pending.draft_id != draft_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "E_COMMIT_TOKEN_SCOPE",
                "message": f"commit_token 属于草案 {pending.draft_id}，不能用于 {draft_id}",
            },
        )
    if pending.is_expired(now):
        runtime.sessions.decide(pending, status="expired", reason="commit_token 已过期")
        raise HTTPException(
            status_code=410,
            detail={"code": "E_COMMIT_TOKEN_EXPIRED", "message": "commit_token 已过期"},
        )
    if not pending.is_pending:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "E_COMMIT_TOKEN_CONSUMED",
                "message": f"commit_token 已被使用（{pending.status}）",
            },
        )
    if fingerprint_draft(pending.draft) != pending.draft_fingerprint:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "E_FINGERPRINT_MISMATCH",
                "message": "确认的草案内容与预览时不一致，拒绝写入",
            },
        )
    return session, pending


def _commit_frames(
    runtime: AppRuntime,
    session: Session,
    pending: PendingCommit,
    *,
    note: str,
) -> list[str]:
    """Resume ``human_gate -> commit`` and render the confirm SSE frames.

    The graph is executed synchronously before the response is returned; the
    commit itself is a short local file transaction, and this keeps the
    tracer's contextvar create/reset in one context.
    """

    stream = SSEStream()
    trace_id = new_trace_id()
    frames = [stream.meta(trace_id, session.session_id).encode()]
    state: dict[str, Any] = dict(pending.state)
    state["human_decision"] = {
        "status": "confirmed",
        "draft_id": pending.draft_id,
        "note": note,
    }
    started = time.perf_counter()
    final: dict[str, Any] = state
    last_stage = ""
    error_sent = False
    failed = False
    try:
        with (
            trace(trace_id),
            runtime.tracer.span(
                "api.confirm",
                trace_id=trace_id,
                attrs={
                    "session_id": session.session_id,
                    "draft_id": pending.draft_id,
                    "target_path": pending.target_path,
                },
            ) as root,
        ):
            for step in runtime.get_graph().stream(state, start="human_gate"):
                final = dict(step.state)
                update = step.update
                stage = str(update.get("stage") or "")
                if stage and stage != last_stage:
                    last_stage = stage
                    frames.append(stream.status(stage, STAGE_LABELS.get(stage, stage)).encode())
                for event in _events_for_update(stream, runtime, session, final, update):
                    if event.event.value == "error":
                        error_sent = True
                    frames.append(event.encode())
            root.set_attr("final_stage", str(final.get("stage", "")))
    except Exception as exc:  # noqa: BLE001 - stream must terminate with an error event
        failed = True
        logger.exception("confirm stream failed: %s", exc)
        if not error_sent:
            error_sent = True
            frames.append(
                stream.error(
                    "E_INTERNAL",
                    redact_text(str(exc)),
                    last_stage or "commit",
                ).encode()
            )
    finally:
        state_error = final.get("error")
        if state_error and not error_sent:
            frames.append(
                stream.error(
                    str(state_error.get("code", "E_INTERNAL")),
                    str(state_error.get("message", "")),
                    str(state_error.get("stage", last_stage)),
                ).encode()
            )
        stage = str(final.get("stage") or "")
        commit = final.get("commit")
        done_status = {
            "committed": "committed",
            "rejected": "rejected",
            "cancelled": "cancelled",
        }.get(stage, "error" if failed or state_error else "done")
        if done_status == "committed" and isinstance(commit, Mapping):
            runtime.sessions.decide(
                pending,
                status="committed",
                note=note,
                commit=commit,
            )
            session.add_message(
                "assistant",
                f"已提交草案 {pending.draft_id}"
                f"（commit_id={commit.get('commit_id', '')}）。",
            )
        elif done_status in {"rejected", "cancelled"}:
            runtime.sessions.decide(
                pending,
                status="rejected" if done_status == "rejected" else "cancelled",
                reason=str((state_error or {}).get("code", "")),
                note=note,
            )
            session.add_message(
                "assistant",
                f"草案未提交：{done_status}（{(state_error or {}).get('code', '')}）",
            )
        session.remember_trace(trace_id, done_status)
        frames.append(
            stream.done(
                status=done_status,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                tokens=int(final.get("tokens", 0) or 0),
                cost_cny=float(final.get("cost_cny", 0.0) or 0.0),
            ).encode()
        )
    return frames


async def _event_stream(
    runtime: AppRuntime,
    session: Session,
    message: str,
) -> AsyncIterator[str]:
    """Run one graph in a worker thread and relay events as an async SSE stream.

    The graph (and the tracer's contextvars) must execute in a single context:
    Starlette advances sync generators in a threadpool, and a ContextVar token
    created in one copied context cannot be reset in another. A dedicated worker
    thread plus an ``asyncio.Queue`` keeps the whole graph in one context while
    still streaming frames to the client as nodes complete.
    """

    stream = SSEStream()
    trace_id = new_trace_id()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(frame: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, frame)

    yield stream.meta(trace_id, session.session_id).encode()

    def run() -> None:
        state: AgentState | Mapping[str, Any] = new_state(
            message,
            trace_id=trace_id,
            session_id=session.session_id,
            designer_id=session.designer_id,
        )
        started = time.perf_counter()
        last_stage = ""
        failed = False
        error_sent = False
        try:
            with (
                trace(trace_id),
                runtime.tracer.span(
                    "api.messages",
                    trace_id=trace_id,
                    attrs={
                        "session_id": session.session_id,
                        "designer_id": session.designer_id,
                        "request_chars": len(message),
                    },
                ) as root,
            ):
                graph = runtime.get_graph()
                for step in graph.stream(state):
                    state = step.state
                    update = step.update
                    stage = str(update.get("stage") or "")
                    if stage and stage != last_stage:
                        last_stage = stage
                        extra: dict[str, Any] = {}
                        if "answer" in update:
                            extra["answer"] = update["answer"]
                        if "citations" in update:
                            extra["citations"] = list(update["citations"])
                        emit(
                            stream.status(
                                stage,
                                STAGE_LABELS.get(stage, stage),
                                **extra,
                            ).encode()
                        )
                    for event in _events_for_update(
                        stream, runtime, session, state, update
                    ):
                        if event.event.value == "error":
                            error_sent = True
                        emit(event.encode())
                root.set_attr("final_stage", str(state.get("stage", "")))
        except Exception as exc:  # noqa: BLE001 - stream must terminate with an error event
            failed = True
            logger.exception("SSE stream failed: %s", exc)
            if not error_sent:
                error_sent = True
                emit(
                    stream.error(
                        "E_INTERNAL",
                        redact_text(str(exc)),
                        last_stage or "unknown",
                    ).encode()
                )
        finally:
            state_error = state.get("error")
            if state_error and not error_sent:
                error_sent = True
                emit(
                    stream.error(
                        str(state_error.get("code", "E_INTERNAL")),
                        str(state_error.get("message", "")),
                        str(state_error.get("stage", last_stage)),
                    ).encode()
                )
            final_status = "error" if failed or state_error else _final_status(state)
            answer = str(state.get("answer") or "")
            session.add_message("user", message)
            session.add_message(
                "assistant",
                answer
                or (
                    "草案已生成，等待确认。"
                    if state.get("preview")
                    else str(state.get("stage", ""))
                ),
            )
            session.remember_trace(trace_id, final_status)
            emit(
                stream.done(
                    status=final_status,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    tokens=int(state.get("tokens", 0) or 0),
                    cost_cny=float(state.get("cost_cny", 0.0) or 0.0),
                ).encode()
            )
            emit(None)

    worker = threading.Thread(target=run, name="gcc-sse-worker", daemon=True)
    worker.start()
    while True:
        frame = await queue.get()
        if frame is None:
            break
        yield frame
    worker.join(timeout=5.0)


def _events_for_update(
    stream: SSEStream,
    runtime: AppRuntime,
    session: Session,
    state: Mapping[str, Any],
    update: Mapping[str, Any],
) -> list[SSEEvent]:
    events: list[SSEEvent] = []
    if "evidence" in update:
        items = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in update["evidence"]
        ]
        events.append(stream.evidence(items))
    for warning in update.get("warnings") or []:
        if isinstance(warning, Mapping):
            events.append(
                stream.warning(
                    str(warning.get("code", "W_CONTEXT_TRUNCATED")),
                    str(warning.get("message", "")),
                )
            )
    if "draft" in update:
        events.append(
            stream.draft(
                update["draft"],  # type: ignore[arg-type]
                int(update.get("draft_version", 1)),
            )
        )
    if "report" in update:
        report = update["report"] or {}
        events.append(
            stream.validation(
                bool(report.get("ok")),
                list(report.get("errors") or []),
                list(report.get("warnings") or []),
                checked=report.get("checked", {}),
            )
        )
    if "preview" in update and update.get("preview") and update.get("stage") != "rejected":
        preview = update["preview"] or {}
        try:
            pending, expires_in = runtime.sessions.open_commit(
                session,
                draft=state.get("draft") or {},
                preview=preview,
                state=state,
            )
        except ValueError as exc:
            events.append(stream.error("E_PREVIEW_REQUIRED", str(exc), "previewed"))
        else:
            events.append(
                stream.awaiting_confirmation(
                    commit_token=pending.token,
                    diff=preview.get("diff") or {},
                    target_path=str(preview.get("target_path", "")),
                    expires_in=expires_in,
                    draft_id=pending.draft_id,
                )
            )
    commit = update.get("commit")
    if isinstance(commit, Mapping):
        events.append(
            stream.committed(
                commit_id=str(commit.get("commit_id", "")),
                backup=commit.get("backup"),
                revision=commit.get("revision") or 0,
            )
        )
    return events


def _final_status(state: AgentState | Mapping[str, Any]) -> str:
    """Map the terminal graph state to the ``done.status`` value."""

    stage = str(state.get("stage") or "")
    if stage == "rejected":
        return "rejected"
    if state.get("error"):
        return "error"
    if stage == "awaiting_confirmation" or state.get("preview"):
        return "awaiting_confirmation"
    return stage or "done"
