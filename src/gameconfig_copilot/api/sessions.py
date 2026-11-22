"""In-memory session store with create / resume / timeout semantics (S2-05).

Sessions keep the conversation transcript, the last trace id and the **pending
commit** entries issued by the SSE ``awaiting_confirmation`` event. A pending
entry is what makes the two-phase write safe (S5-06 / S5-07):

* the ``commit_token`` is opaque and bound server-side to
  ``draft_id + fingerprint + expires_at`` (docs/02 §10.2);
* the server keeps the exact draft/preview/state that was confirmed, so the
  confirm endpoint can resume ``human_gate -> commit`` without re-drafting;
* a second server-side check compares the stored draft digest and expiry before
  the MCP ``commit_draft`` tool runs its own fingerprint/revision checks.

Storage is process-local and intentionally boring: one dict plus a TTL. The
clock is injectable so timeout behaviour is tested without sleeping.

Input
    Optional designer id; session id on lookup/resume; preview results on
    ``open_commit``.
Output
    :class:`Session` / :class:`PendingCommit` snapshots; ``expires_in`` seconds.
Failure semantics
    Unknown id -> :class:`SessionNotFoundError` (HTTP 404); expired id ->
    :class:`SessionExpiredError` (HTTP 410). Expired sessions are not silently
    resurrected; an unknown/expired/consumed token is rejected by the route.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..agents.preview import fingerprint_draft

__all__ = [
    "DEFAULT_COMMIT_TTL_S",
    "DEFAULT_SESSION_TTL_S",
    "PendingCommit",
    "Session",
    "SessionExpiredError",
    "SessionNotFoundError",
    "SessionStore",
]

DEFAULT_SESSION_TTL_S = 1800.0
DEFAULT_COMMIT_TTL_S = 600.0

PENDING_STATUSES = ("awaiting_confirmation", "committed", "rejected", "expired", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionNotFoundError(KeyError):
    """No session with that id exists."""


class SessionExpiredError(RuntimeError):
    """The session exists but its TTL elapsed."""


@dataclass
class PendingCommit:
    """One previewed draft waiting for (or holding) human confirmation."""

    draft_id: str
    token: str
    fingerprint: str
    draft_fingerprint: str
    expires_at: datetime
    draft: dict[str, Any]
    preview: dict[str, Any]
    state: dict[str, Any]
    target_path: str = ""
    base_revision: str = ""
    status: str = "awaiting_confirmation"
    reason: str = ""
    note: str = ""
    commit: dict[str, Any] | None = None
    decided_at: datetime | None = None

    def expires_in(self, now: datetime) -> int:
        return max(0, int((self.expires_at - now).total_seconds()))

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    @property
    def is_pending(self) -> bool:
        return self.status == "awaiting_confirmation"


@dataclass
class Session:
    """One designer conversation."""

    session_id: str
    designer_id: str
    created_at: datetime
    last_seen: datetime
    expires_at: datetime
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_trace_id: str | None = None
    last_stage: str = ""
    pending_commits: dict[str, PendingCommit] = field(default_factory=dict)

    def expires_in(self, now: datetime) -> int:
        return max(0, int((self.expires_at - now).total_seconds()))

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def remember_trace(self, trace_id: str, stage: str) -> None:
        self.last_trace_id = trace_id
        self.last_stage = stage


class SessionStore:
    """TTL session registry plus the pending-commit token index."""

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_SESSION_TTL_S,
        commit_ttl_s: float = DEFAULT_COMMIT_TTL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s 必须 > 0")
        if commit_ttl_s <= 0:
            raise ValueError("commit_ttl_s 必须 > 0")
        self.ttl_s = float(ttl_s)
        self.commit_ttl_s = float(commit_ttl_s)
        self._clock = clock or _utcnow
        self._sessions: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def now(self) -> datetime:
        return self._clock()

    def create(self, designer_id: str | None = None) -> Session:
        now = self.now()
        session = Session(
            session_id=f"sess_{uuid.uuid4().hex}",
            designer_id=(designer_id or "anonymous").strip() or "anonymous",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(seconds=self.ttl_s),
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        now = self.now()
        if now >= session.expires_at:
            raise SessionExpiredError(session_id)
        session.last_seen = now
        return session

    def resume(self, session_id: str) -> Session:
        """Recover a live session; refresh its TTL."""

        session = self.get(session_id)
        session.expires_at = self.now() + timedelta(seconds=self.ttl_s)
        return session

    # ------------------------------------------------------------ commit flow
    def open_commit(
        self,
        session: Session,
        *,
        draft: Mapping[str, Any],
        preview: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> tuple[PendingCommit, int]:
        """Issue a token for a previewed draft and remember it for confirmation."""

        draft_id = str(preview.get("draft_id") or "").strip()
        fingerprint = str(preview.get("fingerprint") or "").strip()
        if not draft_id or not fingerprint:
            raise ValueError("preview 必须包含 draft_id 与 fingerprint")
        now = self.now()
        pending = PendingCommit(
            draft_id=draft_id,
            token=f"ct_{secrets.token_urlsafe(24)}",
            fingerprint=fingerprint,
            draft_fingerprint=fingerprint_draft(draft),
            expires_at=now + timedelta(seconds=self.commit_ttl_s),
            draft=dict(draft),
            preview=dict(preview),
            state=dict(state),
            target_path=str(preview.get("target_path") or ""),
            base_revision=str(
                preview.get("base_revision") or preview.get("revision") or ""
            ),
        )
        session.pending_commits[draft_id] = pending
        return pending, int(self.commit_ttl_s)

    def find_pending(self, token: str) -> tuple[Session, PendingCommit] | None:
        """Resolve a token to ``(session, pending)``; ``None`` when unknown."""

        needle = str(token or "").strip()
        if not needle:
            return None
        for session in self._sessions.values():
            for pending in session.pending_commits.values():
                if pending.token == needle:
                    return session, pending
        return None

    def decide(
        self,
        pending: PendingCommit,
        *,
        status: str,
        reason: str = "",
        note: str = "",
        commit: Mapping[str, Any] | None = None,
    ) -> PendingCommit:
        """Record the human decision / commit outcome on the pending entry."""

        if status not in PENDING_STATUSES:
            raise ValueError(f"未知的 pending 状态：{status!r}")
        pending.status = status
        pending.reason = reason
        pending.note = note
        pending.commit = dict(commit) if commit is not None else None
        pending.decided_at = self.now()
        return pending
