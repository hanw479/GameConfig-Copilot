"""SQLite session checkpointer: messages, draft versions, stage, 7-day TTL (S6-01).

Owner: Memory Worker Mem (``docs/06-multi-agent-collaboration.md`` §5.2). This
module stores conversation state only; it never imports ``agents``/``api`` and
has no LangGraph or LLM dependency (``docs/02-architecture.md`` §2/§8).

Contract
--------
Input
    A :class:`SessionState` (or an equivalent mapping): ``session_id``,
    ``designer_id``, ``stage``, the message log, the current draft and its
    version. ``db_path`` and ``ttl_s`` are fixed at construction; the clock is
    injectable so TTL behaviour is tested without sleeping.
Output
    A detached :class:`SessionState` snapshot. Mutating the returned object does
    not touch the database; call :meth:`SessionStore.put` to persist it.
Failure semantics
    * unknown id -> :class:`SessionNotFoundError` (matches ``api/sessions.py``);
    * known but expired id -> :class:`SessionExpiredError`; expiry never
      resurrects a session (``put`` on an expired id starts a fresh one);
    * invalid arguments -> :class:`SessionValueError`;
    * a session row, its messages and its draft history are deleted together by
      :meth:`expire` (SQLite ``ON DELETE CASCADE``).

Persistence and concurrency
---------------------------
Every operation opens a short-lived connection with ``foreign_keys=ON`` and a
busy timeout, and writes happen inside ``BEGIN IMMEDIATE``. That makes
:meth:`append_message` safe across threads and processes (``seq`` is allocated
inside the transaction), and lets a new process resume the same database after
a restart. ``put`` is a full-snapshot write: it replaces the message log and the
current draft, which is what a graph checkpointer does on every super-step.
Draft versions are append-only, so an earlier revision can still be inspected.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_SESSION_TTL_S",
    "MemoryMessage",
    "SessionExpiredError",
    "SessionMemoryError",
    "SessionNotFoundError",
    "SessionState",
    "SessionStore",
    "SessionValueError",
]

DEFAULT_SESSION_TTL_S = 7 * 24 * 60 * 60
"""``docs/02`` §8: session state lives for 7 days."""

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "memory" / "sessions.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    designer_id TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    draft_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires
    ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL
        REFERENCES sessions (session_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS drafts (
    session_id TEXT NOT NULL
        REFERENCES sessions (session_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    draft TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, version)
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """Normalise to aware UTC; naive values are assumed to already be UTC."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat()


def _parse(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Path):
        return str(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)


def _dump_draft(draft: Mapping[str, Any]) -> str:
    return json.dumps(
        draft,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


class SessionMemoryError(RuntimeError):
    """Base class for session-memory failures."""


class SessionNotFoundError(KeyError):
    """No session with that id exists (or it was purged by ``expire``)."""


class SessionExpiredError(SessionMemoryError):
    """The session exists but its 7-day TTL elapsed."""


class SessionValueError(SessionMemoryError, ValueError):
    """A caller-supplied session field is malformed."""


@dataclass
class MemoryMessage:
    """One transcript entry; ``seq`` is 1-based and allocated by the store."""

    role: str
    content: str
    created_at: datetime = field(default_factory=_utcnow)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "role": self.role,
            "content": self.content,
            "created_at": _iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MemoryMessage:
        return cls(
            role=str(payload.get("role", "")),
            content=str(payload.get("content", "")),
            created_at=_parse(str(payload["created_at"]))
            if payload.get("created_at")
            else _utcnow(),
            seq=int(payload.get("seq", 0) or 0),
        )


@dataclass
class SessionState:
    """Detached snapshot of one designer conversation."""

    session_id: str
    designer_id: str = "anonymous"
    stage: str = ""
    messages: list[MemoryMessage] = field(default_factory=list)
    draft: dict[str, Any] | None = None
    draft_version: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    def add_message(self, role: str, content: str) -> MemoryMessage:
        """Append to this detached snapshot (persist with ``put`` or the store)."""

        message = MemoryMessage(
            role=role,
            content=content,
            created_at=_utcnow(),
            seq=len(self.messages) + 1,
        )
        self.messages.append(message)
        return message

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "designer_id": self.designer_id,
            "stage": self.stage,
            "messages": [message.to_dict() for message in self.messages],
            "draft": self.draft,
            "draft_version": self.draft_version,
            "created_at": _iso(self.created_at) if self.created_at else None,
            "updated_at": _iso(self.updated_at) if self.updated_at else None,
            "expires_at": _iso(self.expires_at) if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SessionState:
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, Sequence) or isinstance(
            raw_messages, (str, bytes)
        ):
            raw_messages = ()
        raw_draft = payload.get("draft")
        return cls(
            session_id=str(payload.get("session_id", "")),
            designer_id=str(payload.get("designer_id") or "anonymous"),
            stage=str(payload.get("stage") or ""),
            messages=[
                MemoryMessage.from_dict(item)
                if isinstance(item, Mapping)
                else item
                for item in raw_messages
            ],
            draft=dict(raw_draft) if isinstance(raw_draft, Mapping) else None,
            draft_version=int(payload.get("draft_version", 0) or 0),
            created_at=_parse(str(payload["created_at"]))
            if payload.get("created_at")
            else None,
            updated_at=_parse(str(payload["updated_at"]))
            if payload.get("updated_at")
            else None,
            expires_at=_parse(str(payload["expires_at"]))
            if payload.get("expires_at")
            else None,
        )


def _require_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise SessionValueError(f"session_id 必须是非空字符串：{session_id!r}")
    return session_id


def _coerce_state(value: SessionState | Mapping[str, Any]) -> SessionState:
    if isinstance(value, SessionState):
        return value
    if isinstance(value, Mapping):
        return SessionState.from_dict(value)
    raise SessionValueError(f"state 必须是 SessionState 或映射：{type(value).__name__}")


def _coerce_draft(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        value = dump(mode="json")
    if not isinstance(value, Mapping):
        raise SessionValueError(f"draft 必须是映射或 TaskDraft：{type(value).__name__}")
    return dict(value)


def _message_rows(
    messages: Sequence[MemoryMessage],
    now: datetime,
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for message in messages:
        if isinstance(message, Mapping):
            message = MemoryMessage.from_dict(message)
        if not isinstance(message.role, str) or not message.role.strip():
            raise SessionValueError("消息 role 必须是非空字符串")
        if not isinstance(message.content, str):
            raise SessionValueError("消息 content 必须是字符串")
        rows.append(
            (
                message.role,
                message.content,
                _iso(message.created_at or now),
            )
        )
    return rows


class SessionStore:
    """SQLite-backed session checkpointer (S6-01)."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        ttl_s: float = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], datetime] | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        if isinstance(ttl_s, bool) or ttl_s <= 0:
            raise SessionValueError("ttl_s 必须 > 0")
        if isinstance(timeout_s, bool) or timeout_s <= 0:
            raise SessionValueError("timeout_s 必须 > 0")
        self.db_path = (
            Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_s = float(ttl_s)
        self.timeout_s = float(timeout_s)
        self._clock = clock or _utcnow
        self._init_schema()

    # -- lifecycle -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=self.timeout_s)
        connection.row_factory = sqlite3.Row
        connection.isolation_level = None  # explicit BEGIN/COMMIT below
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def now(self) -> datetime:
        return _as_utc(self._clock())

    # -- reads ---------------------------------------------------------------

    def get(self, session_id: str) -> SessionState:
        """Load one live session snapshot.

        Raises
        ------
        SessionNotFoundError
            Unknown id or already purged.
        SessionExpiredError
            The stored TTL elapsed; call :meth:`expire` to reclaim the rows.
        """

        sid = _require_id(session_id)
        with self._read() as connection:
            state = self._load_state(connection, sid)
        if state is None:
            raise SessionNotFoundError(sid)
        if self.now() >= _as_utc(state.expires_at or self.now()):
            raise SessionExpiredError(sid)
        return state

    def get_or_none(self, session_id: str) -> SessionState | None:
        """Like :meth:`get` but returns ``None`` for unknown/expired ids."""

        try:
            return self.get(session_id)
        except (SessionNotFoundError, SessionExpiredError):
            return None

    def exists(self, session_id: str) -> bool:
        """True when the session exists and has not expired."""

        return self.get_or_none(session_id) is not None

    def draft_history(self, session_id: str) -> list[dict[str, Any]]:
        """All retained draft versions, newest first (append-only audit)."""

        state = self.get(session_id)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT version, draft, created_at FROM drafts "
                "WHERE session_id = ? ORDER BY version DESC",
                (state.session_id,),
            ).fetchall()
        return [
            {
                "version": int(row["version"]),
                "draft": json.loads(row["draft"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # -- writes --------------------------------------------------------------

    def put(
        self,
        state: SessionState | Mapping[str, Any],
        *,
        ttl_s: float | None = None,
    ) -> SessionState:
        """Create or replace a session snapshot (checkpointer save).

        The message log is replaced wholesale and the draft is written as a new
        append-only version (``draft_version`` auto-increments when omitted).
        An expired session is treated as absent, so a resumed id starts fresh.
        """

        snapshot = _coerce_state(state)
        sid = _require_id(snapshot.session_id)
        now = self.now()
        ttl = self.ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            raise ValueError("ttl_s 必须 > 0")
        designer = (snapshot.designer_id or "anonymous").strip() or "anonymous"
        if not isinstance(snapshot.stage, str):
            raise SessionValueError("stage 必须是字符串")
        draft = _coerce_draft(snapshot.draft)
        requested_version = snapshot.draft_version
        if isinstance(requested_version, bool) or not isinstance(requested_version, int):
            raise SessionValueError("draft_version 必须是整数")
        if requested_version < 0:
            raise SessionValueError("draft_version 不能为负")
        message_rows = _message_rows(snapshot.messages, now)

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            if row is not None and now >= _parse(row["expires_at"]):
                connection.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                row = None
            created_at = _parse(row["created_at"]) if row is not None else now
            existing_version = int(row["draft_version"]) if row is not None else 0

            if draft is None:
                version = 0
            elif requested_version > 0:
                version = requested_version
            else:
                version = 1 if existing_version == 0 else existing_version + 1

            if row is None:
                connection.execute(
                    "INSERT INTO sessions (session_id, designer_id, stage, "
                    "draft_version, created_at, updated_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sid,
                        designer,
                        snapshot.stage,
                        version,
                        _iso(created_at),
                        _iso(now),
                        _iso(now + timedelta(seconds=ttl)),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE sessions SET designer_id = ?, stage = ?, draft_version = ?, "
                    "updated_at = ?, expires_at = ? WHERE session_id = ?",
                    (
                        designer,
                        snapshot.stage,
                        version,
                        _iso(now),
                        _iso(now + timedelta(seconds=ttl)),
                        sid,
                    ),
                )

            if draft is not None and version > 0:
                connection.execute(
                    "INSERT OR REPLACE INTO drafts (session_id, version, draft, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (sid, version, _dump_draft(draft), _iso(now)),
                )

            connection.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            connection.executemany(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (sid, index, role, content, created)
                    for index, (role, content, created) in enumerate(message_rows, start=1)
                ],
            )
            refreshed = self._load_state(connection, sid)
        if refreshed is None:  # pragma: no cover - the row was just written
            raise SessionMemoryError(f"写入会话失败：{sid}")
        return refreshed

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> MemoryMessage:
        """Append one transcript entry and slide the session TTL forward."""

        sid = _require_id(session_id)
        if not isinstance(role, str) or not role.strip():
            raise SessionValueError("消息 role 必须是非空字符串")
        if not isinstance(content, str):
            raise SessionValueError("消息 content 必须是字符串")
        now = self.now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            if row is None:
                raise SessionNotFoundError(sid)
            if now >= _parse(row["expires_at"]):
                raise SessionExpiredError(sid)
            seq_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages "
                "WHERE session_id = ?",
                (sid,),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            connection.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, seq, role, content, _iso(now)),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ?, expires_at = ? WHERE session_id = ?",
                (
                    _iso(now),
                    _iso(now + timedelta(seconds=self.ttl_s)),
                    sid,
                ),
            )
        return MemoryMessage(role=role, content=content, created_at=now, seq=seq)

    def set_stage(self, session_id: str, stage: str) -> SessionState:
        """Update the graph stage (``routing`` / ``drafting`` / ...)."""

        sid = _require_id(session_id)
        if not isinstance(stage, str):
            raise SessionValueError("stage 必须是字符串")
        now = self.now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            if row is None:
                raise SessionNotFoundError(sid)
            if now >= _parse(row["expires_at"]):
                raise SessionExpiredError(sid)
            connection.execute(
                "UPDATE sessions SET stage = ?, updated_at = ?, expires_at = ? "
                "WHERE session_id = ?",
                (
                    stage,
                    _iso(now),
                    _iso(now + timedelta(seconds=self.ttl_s)),
                    sid,
                ),
            )
            state = self._load_state(connection, sid)
        if state is None:  # pragma: no cover - the row was just updated
            raise SessionMemoryError(f"读取会话失败：{sid}")
        return state

    def expire(self, now: datetime | None = None) -> int:
        """Delete every session whose TTL elapsed; returns the row count."""

        moment = _as_utc(now) if now is not None else self.now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM sessions WHERE expires_at <= ?",
                (_iso(moment),),
            ).fetchone()
            total = int(row["total"])
            if total:
                connection.execute(
                    "DELETE FROM sessions WHERE expires_at <= ?", (_iso(moment),)
                )
        return total

    # -- internals -----------------------------------------------------------

    def _load_state(
        self, connection: sqlite3.Connection, session_id: str
    ) -> SessionState | None:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        messages = [
            MemoryMessage(
                role=message["role"],
                content=message["content"],
                created_at=_parse(message["created_at"]),
                seq=int(message["seq"]),
            )
            for message in connection.execute(
                "SELECT seq, role, content, created_at FROM messages "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
        ]
        draft: dict[str, Any] | None = None
        version = int(row["draft_version"])
        if version > 0:
            draft_row = connection.execute(
                "SELECT draft FROM drafts WHERE session_id = ? AND version = ?",
                (session_id, version),
            ).fetchone()
            if draft_row is not None:
                draft = json.loads(draft_row["draft"])
        return SessionState(
            session_id=row["session_id"],
            designer_id=row["designer_id"],
            stage=row["stage"],
            messages=messages,
            draft=draft,
            draft_version=version,
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            expires_at=_parse(row["expires_at"]),
        )
