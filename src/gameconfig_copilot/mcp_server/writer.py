"""Atomic, auditable task-config writer (S5-04 / S5-05 / S5-09).

Owner: Write Worker M (``docs/06-multi-agent-collaboration.md`` §5.2). This module
owns the write path only and never imports ``agents``/``api``
(``docs/02-architecture.md`` §2 dependency direction).

Contract
--------
Input
    A draft mapping (or :class:`~gameconfig_copilot.schemas.TaskDraft`), the
    fingerprint produced by :meth:`Writer.preview`, the caller's
    ``expected_revision``, an author and an optional note. Every filesystem root
    derives from ``root`` (repository root) unless explicitly overridden, so tests
    can point the writer at a ``tmp_path`` copy instead of the real ``data/tasks``.
Output
    Plain dicts matching ``docs/02`` §6.3 (TaskConfigView / PreviewResult /
    CommitResult). ``path`` / ``backup_path`` are repository-relative POSIX
    strings so callers can persist them without leaking absolute paths.
Failure semantics
    A refusal raises :class:`WriterError` carrying a frozen ``E_*`` code
    (``docs/01`` §7.4) plus ``details``; the MCP layer renders that as
    ``{"ok": false, "error": {...}}``. No failed commit changes the target file:
    bytes are written to a same-directory ``*.tmp-*`` file, flushed and fsynced,
    then swapped in with :func:`os.replace` (same-volume atomic replace).

Crash safety (``docs/02`` §7)
-----------------------------
* temp file left behind -> :meth:`Writer.prepare` removes it (dead pid or grace);
* crash after ``os.replace`` but before the audit append -> :meth:`prepare`
  compares each task revision with the audit tail and appends a ``recovered``
  record;
* power loss -> ``os.replace`` leaves either the old or the new file, and the
  pre-replace backup under ``data/backups/{task_id}/`` can be rolled back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from gameconfig_copilot.schemas import (
    Constraint,
    ErrorCode,
    TaskDraft,
    ValidationErrorItem,
    ValidationReport,
)
from gameconfig_copilot.schemas.task import TASK_ID_RE

from .validator import (
    DEFAULT_CONSTRAINTS_DIR,
    ConstraintLoadError,
    RepoSnapshot,
    load_constraints,
    validate,
)

__all__ = [
    "DEFAULT_LOCK_TIMEOUT_S",
    "EMPTY_REVISION",
    "FileLock",
    "Writer",
    "WriterError",
    "canonical_json",
    "fingerprint_for",
    "normalize_draft",
    "render_task",
    "revision_of_bytes",
    "revision_of_file",
]

LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOCK_TIMEOUT_S = 5.0
LOCK_STALE_AFTER_S = 300.0
TEMP_GRACE_S = 60.0
O_BINARY = getattr(os, "O_BINARY", 0)
_COMMIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# Validation-related codes come from the frozen enum; transport/lifecycle codes
# are additive here because docs/01 §7.4 only covers draft-rule errors.
E_PATH_DENIED = ErrorCode.PATH_DENIED.value
E_STALE_REVISION = ErrorCode.STALE_REVISION.value
E_FINGERPRINT_MISMATCH = ErrorCode.FINGERPRINT_MISMATCH.value
E_IO = ErrorCode.IO.value
E_NOT_FOUND = "E_NOT_FOUND"
E_INVALID_ARGUMENT = "E_INVALID_ARGUMENT"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_BACKUP_NOT_FOUND = "E_BACKUP_NOT_FOUND"
E_CONSTRAINT_LOAD = "E_CONSTRAINT_LOAD"
E_AUDIT = "E_AUDIT"

# A missing file and an empty file share this revision; preview and commit both
# report it, so a create operation can be fingerprinted before the file exists.
EMPTY_REVISION = hashlib.sha256(b"").hexdigest()[:12]


class WriterError(RuntimeError):
    """Structured write-path refusal (never a bare stack trace for callers)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_envelope(self) -> dict[str, Any]:
        """Render the docs/02 §6.3 error envelope."""

        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def revision_of_bytes(data: bytes) -> str:
    """Content revision used by preview/commit (``docs/02`` §6.4)."""

    return hashlib.sha256(data).hexdigest()[:12]


def revision_of_file(path: Path) -> str:
    """Revision of an existing file; a missing file maps to ``EMPTY_REVISION``."""

    try:
        return revision_of_bytes(path.read_bytes())
    except FileNotFoundError:
        return EMPTY_REVISION
    except OSError as exc:
        raise WriterError(E_IO, f"读取文件失败：{exc}", {"path": str(path)}) from exc


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for fingerprinting (sorted keys, no whitespace)."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def normalize_draft(draft: Any) -> dict[str, Any]:
    """Accept a mapping or a TaskDraft-like model and return a plain dict."""

    if isinstance(draft, Mapping):
        return dict(draft)
    dump = getattr(draft, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")
        if isinstance(payload, Mapping):
            return dict(payload)
    raise WriterError(
        E_INVALID_ARGUMENT,
        "draft 必须是映射或 TaskDraft",
        {"type": type(draft).__name__},
    )


def render_task(draft: Any) -> str:
    """Render a draft as the on-disk JSON document (2-space indent, newline).

    The payload is rendered as supplied (not re-dumped from the Pydantic model)
    so a commit only changes the fields the author touched instead of injecting
    explicit ``null`` defaults into the corpus.
    """

    payload = normalize_draft(draft)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n"


def fingerprint_for(draft: Any, target_path: str, base_revision: str) -> str:
    """``sha256(canonical{draft,target_path,base_revision})[:16]`` (docs/02 §6.4)."""

    payload = {
        "draft": normalize_draft(draft),
        "target_path": target_path,
        "base_revision": base_revision,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def _utc_stamp(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    return current.strftime("%Y%m%dT%H%M%SZ")


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _try_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - best-effort cleanup
        LOGGER.warning("Failed to remove temporary file %s: %s", path, exc)


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe used to break locks/temps of dead writers."""

    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - platform specific
        try:
            import ctypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(process_query_limited_information, 0, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - never steal a lock on a probe error
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _pid_from_temp_name(name: str) -> int | None:
    marker = ".tmp-"
    if marker not in name:
        return None
    head = name.split(marker, 1)[1].split("-", 1)[0]
    return int(head) if head.isdigit() else None


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync; unsupported on Windows by design."""

    if not hasattr(os, "O_DIRECTORY"):  # pragma: no cover - Windows
        return
    try:  # pragma: no cover - POSIX durability hardening
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover - filesystem without dir fsync
        return


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` through a same-directory temp file + fsync + os.replace."""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp-{os.getpid()}-{uuid4().hex[:12]}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except OSError as exc:
        _try_unlink(tmp)
        raise WriterError(
            E_IO,
            f"写入失败，目标文件未被替换：{exc}",
            {"target_path": str(target), "temp_path": str(tmp)},
        ) from exc
    except BaseException:
        _try_unlink(tmp)
        raise


def _diff_stats(diff: str) -> dict[str, int]:
    added = sum(
        1
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return {"added": added, "removed": removed}


def _unified_diff(old_text: str, new_text: str, relative_path: str) -> str:
    return "".join(
        unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def _schema_report(exc: ValidationError) -> ValidationReport:
    """Convert a Pydantic failure into the frozen ``ValidationReport`` shape."""

    items: list[ValidationErrorItem] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "draft"
        missing = error.get("type") == "missing"
        items.append(
            ValidationErrorItem(
                code=ErrorCode.SCHEMA_MISSING_FIELD
                if missing
                else ErrorCode.SCHEMA_INVALID_VALUE,
                field=location,
                message=f"{location}: {error.get('msg', '非法取值')}",
            )
        )
    return ValidationReport(ok=False, errors=items)


class FileLock:
    """Cooperative single-file lock with dead-owner takeover (``docs/02`` §7).

    Portable implementation: ``O_CREAT | O_EXCL`` create with a JSON owner
    payload, bounded polling, and takeover when the recorded pid is gone or the
    lock is older than ``stale_after_s``. Release only removes a lock we own
    (token match), so two writers can never delete each other's lock.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
        poll_s: float = 0.02,
        stale_after_s: float = LOCK_STALE_AFTER_S,
    ) -> None:
        self.path = path
        self.timeout_s = max(0.05, float(timeout_s))
        self.poll_s = max(0.005, float(poll_s))
        self.stale_after_s = max(1.0, float(stale_after_s))
        self._token = uuid4().hex
        self._held = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.release()
        return False

    def acquire(self) -> None:
        """Block until acquired; raise ``E_LOCK_TIMEOUT`` after ``timeout_s``."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY,
                    0o644,
                )
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise WriterError(
                        E_LOCK_TIMEOUT,
                        f"获取文件锁超时（{self.timeout_s:.1f}s）：{self.path.name}",
                        {
                            "lock_path": str(self.path),
                            "timeout_s": self.timeout_s,
                            "hint": "另一个写入进程仍持有锁；确认其存活后再重试",
                        },
                    ) from None
                time.sleep(self.poll_s)
                continue
            payload = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "token": self._token,
                "created_at": _utc_stamp(),
            }
            try:
                try:
                    os.write(fd, canonical_json(payload).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                _try_unlink(self.path)
                raise WriterError(
                    E_IO,
                    f"写入锁文件失败：{exc}",
                    {"lock_path": str(self.path)},
                ) from exc
            self._held = True
            return

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        owner = self._read_owner()
        if owner is not None and owner.get("token") != self._token:
            LOGGER.warning("Lock %s was taken over; not removing it", self.path)
            return
        _try_unlink(self.path)

    def _read_owner(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _break_if_stale(self) -> bool:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return True
        except OSError:  # pragma: no cover - transient filesystem error
            return False
        owner = self._read_owner()
        now = time.time()
        if owner is None:
            if now - stat.st_mtime > self.stale_after_s:
                _try_unlink(self.path)
                return True
            return False
        same_host = owner.get("host") == socket.gethostname()
        pid = owner.get("pid")
        if same_host and isinstance(pid, int) and not _pid_alive(pid):
            _try_unlink(self.path)
            return True
        if now - stat.st_mtime > self.stale_after_s:
            _try_unlink(self.path)
            return True
        return False


@dataclass(frozen=True)
class _TaskFile:
    path: Path
    relative_path: str
    payload: Mapping[str, Any]
    revision: str
    mtime: float


class Writer:
    """Path-guarded, locked, atomic writer for ``data/tasks/*.json``."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        tasks_dir: str | os.PathLike[str] | None = None,
        backups_dir: str | os.PathLike[str] | None = None,
        locks_dir: str | os.PathLike[str] | None = None,
        audit_path: str | os.PathLike[str] | None = None,
        constraints: Sequence[Constraint] | None = None,
        constraints_dir: str | os.PathLike[str] | None = None,
        lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
        temp_grace_s: float = TEMP_GRACE_S,
    ) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else REPO_ROOT
        self.tasks_dir = self._resolve(tasks_dir, self.root / "data" / "tasks")
        self.backups_dir = self._resolve(backups_dir, self.root / "data" / "backups")
        self.locks_dir = self._resolve(locks_dir, self.root / "data" / ".locks")
        self.audit_path = self._resolve(audit_path, self.root / "traces" / "commits.jsonl")
        self.lock_timeout_s = max(0.05, float(lock_timeout_s))
        self.temp_grace_s = max(0.0, float(temp_grace_s))
        self._injected_constraints = list(constraints) if constraints is not None else None
        self._constraints_dir = (
            Path(constraints_dir).expanduser().resolve() if constraints_dir else None
        )
        self._constraints_cache: list[Constraint] | None = None
        self._audit_lock = threading.Lock()

    @staticmethod
    def _resolve(value: str | os.PathLike[str] | None, default: Path) -> Path:
        if value is None:
            return default.resolve()
        return Path(value).expanduser().resolve()

    # -- paths ---------------------------------------------------------------

    def relative(self, path: Path) -> str:
        """Repository-relative POSIX path when possible (absolute otherwise)."""

        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).resolve().as_posix()

    def guard_path(self, raw: str) -> Path:
        """Resolve ``raw`` under ``data/tasks/`` or raise ``E_PATH_DENIED``.

        Rejects absolute paths / drive prefixes / UNC paths, any ``..`` segment,
        non-``.json`` names, symlinked components and anything whose canonical
        location escapes the tasks directory.
        """

        if not isinstance(raw, str) or not raw.strip():
            raise WriterError(
                E_PATH_DENIED,
                "写入路径为空或类型非法",
                {"path": repr(raw)},
            )
        text = raw.strip()
        if "\x00" in text:
            raise WriterError(E_PATH_DENIED, "写入路径包含 NUL 字节", {"path": repr(raw)})
        drive, _ = os.path.splitdrive(text)
        normalized = text.replace("\\", "/")
        if drive or normalized.startswith("/"):
            raise WriterError(
                E_PATH_DENIED,
                f"拒绝绝对路径：{text}（只允许 data/tasks/**）",
                {"path": text},
            )
        # Accept both "T_SIDE_0213.json" and "data/tasks/T_SIDE_0213.json".
        tasks_prefix = self.relative(self.tasks_dir).strip("/")
        if tasks_prefix not in ("", "."):
            prefix = f"{tasks_prefix}/"
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
        segments = [segment for segment in normalized.split("/") if segment not in ("", ".")]
        if not segments:
            raise WriterError(E_PATH_DENIED, "写入路径为空", {"path": text})
        if any(segment == ".." for segment in segments):
            raise WriterError(
                E_PATH_DENIED,
                f"拒绝包含 .. 的路径：{text}",
                {"path": text},
            )
        if Path(segments[-1]).suffix.lower() != ".json":
            raise WriterError(
                E_PATH_DENIED,
                f"只允许写入 .json 文件：{text}",
                {"path": text},
            )
        root = self.tasks_dir.resolve()
        candidate = (root / Path(*segments)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise WriterError(
                E_PATH_DENIED,
                f"路径逃逸 data/tasks：{text}",
                {"path": text, "resolved": str(candidate), "root": str(root)},
            ) from exc
        if candidate == root:
            raise WriterError(E_PATH_DENIED, "目标不能是目录本身", {"path": text})
        current = root
        for segment in segments:
            current = current / segment
            if current.is_symlink():
                raise WriterError(
                    E_PATH_DENIED,
                    f"拒绝符号链接路径：{text}",
                    {"path": text, "link": str(current)},
                )
        return candidate

    def target_for_task_id(self, task_id: str) -> Path:
        """Map a validated task id to ``data/tasks/{task_id}.json``."""

        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise WriterError(
                E_PATH_DENIED,
                f"task_id 非法，拒绝构造写入路径：{task_id!r}",
                {"task_id": repr(task_id)},
            )
        return self.guard_path(f"{task_id}.json")

    def lock(self, task_id: str) -> FileLock:
        """Per-task file lock at ``data/.locks/{task_id}.lock``."""

        return FileLock(
            self.locks_dir / f"{task_id}.lock",
            timeout_s=self.lock_timeout_s,
        )

    # -- constraints / snapshot ---------------------------------------------

    @property
    def constraints(self) -> list[Constraint]:
        if self._injected_constraints is not None:
            return self._injected_constraints
        if self._constraints_cache is None:
            directory = self._constraints_dir or self._default_constraints_dir()
            try:
                self._constraints_cache = load_constraints(directory=directory)
            except ConstraintLoadError as exc:
                raise WriterError(
                    E_CONSTRAINT_LOAD,
                    f"约束加载失败，拒绝校验/写入（fail-closed）：{exc}",
                    {"constraints_dir": str(directory)},
                ) from exc
        return self._constraints_cache

    def _default_constraints_dir(self) -> Path:
        candidate = self.root / "data" / "constraints"
        return candidate if candidate.is_dir() else DEFAULT_CONSTRAINTS_DIR

    def _task_files(self) -> list[_TaskFile]:
        files: list[_TaskFile] = []
        if not self.tasks_dir.is_dir():
            return files
        for path in sorted(self.tasks_dir.rglob("*.json")):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise WriterError(
                    E_IO,
                    f"读取任务文件失败：{exc}",
                    {"path": self.relative(path)},
                ) from exc
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WriterError(
                    E_IO,
                    f"任务文件 JSON 非法，拒绝继续：{exc}",
                    {"path": self.relative(path)},
                ) from exc
            if not isinstance(payload, Mapping):
                raise WriterError(
                    E_IO,
                    "任务文件顶层必须是 JSON 对象",
                    {"path": self.relative(path)},
                )
            files.append(
                _TaskFile(
                    path=path,
                    relative_path=self.relative(path),
                    payload=payload,
                    revision=revision_of_bytes(data),
                    mtime=path.stat().st_mtime,
                )
            )
        return files

    def snapshot(self, *, drop_task_id: str | None = None) -> RepoSnapshot:
        """Repository projection for conflict rules (pure data, no writes).

        Entity IDs are harvested from every file, including the one being
        updated, so an unchanged ``dialogue_ref`` does not look "missing". The
        draft's own task record is dropped only on update, keeping duplicate
        ``task_id`` detection for create operations.
        """

        payloads = [
            {**dict(item.payload), "path": item.relative_path}
            for item in self._task_files()
        ]
        snapshot = RepoSnapshot.from_task_payloads(payloads)
        if drop_task_id is not None and snapshot.tasks is not None:
            remaining = {
                key: value
                for key, value in snapshot.tasks.items()
                if key != drop_task_id
            }
            snapshot = replace(snapshot, tasks=remaining)
        return snapshot

    def validate(self, draft: Any, *, target: Path | None = None) -> ValidationReport:
        """Forced full re-check: YAML rules plus the TaskDraft contract.

        Called by ``commit_draft`` *inside* the lock right before writing; never
        trusts an upstream validation result (docs/03 S5 implementation notes).
        """

        payload = normalize_draft(draft)
        task_id = payload.get("task_id")
        own_task_id: str | None = None
        if isinstance(task_id, str) and TASK_ID_RE.fullmatch(task_id):
            candidate = target or (self.tasks_dir / f"{task_id}.json")
            if candidate.is_file():
                own_task_id = task_id
        report = validate(payload, self.constraints, self.snapshot(drop_task_id=own_task_id))
        if report.ok:
            try:
                TaskDraft.model_validate(payload)
            except ValidationError as exc:
                return _schema_report(exc)
        return report

    # -- read tools ----------------------------------------------------------

    def read_task(self, task_id: str) -> dict[str, Any]:
        """TaskConfigView: ``{task_id, path, revision, content, updated_at}``."""

        target = self.target_for_task_id(task_id)
        data = self._read_bytes(target)
        if data is None:
            raise WriterError(
                E_NOT_FOUND,
                f"任务不存在：{task_id}",
                {"task_id": task_id, "path": self.relative(target)},
            )
        try:
            content = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WriterError(
                E_IO,
                f"任务文件 JSON 非法：{exc}",
                {"task_id": task_id, "path": self.relative(target)},
            ) from exc
        return {
            "task_id": task_id,
            "path": self.relative(target),
            "revision": revision_of_bytes(data),
            "content": content,
            "updated_at": _mtime_iso(target),
        }

    def list_tasks(
        self,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recent tasks, newest file mtime first, with optional filters."""

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise WriterError(E_INVALID_ARGUMENT, "limit 必须是整数", {"limit": repr(limit)})
        if not 1 <= limit <= 100:
            raise WriterError(
                E_INVALID_ARGUMENT,
                "limit 必须在 1..100 之间",
                {"limit": limit},
            )
        raw_filters: Mapping[str, Any] = filters or {}
        if not isinstance(raw_filters, Mapping):
            raise WriterError(
                E_INVALID_ARGUMENT,
                "filters 必须是对象",
                {"type": type(filters).__name__},
            )
        allowed = {"task_type", "tag", "level_min", "level_max", "keyword"}
        unknown = sorted(set(raw_filters) - allowed)
        if unknown:
            raise WriterError(
                E_INVALID_ARGUMENT,
                f"未知过滤字段：{unknown}",
                {"allowed": sorted(allowed)},
            )
        for key in ("task_type", "tag", "keyword"):
            value = raw_filters.get(key)
            if value is not None and not isinstance(value, str):
                raise WriterError(E_INVALID_ARGUMENT, f"filters.{key} 必须是字符串", {"key": key})
        for key in ("level_min", "level_max"):
            value = raw_filters.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise WriterError(E_INVALID_ARGUMENT, f"filters.{key} 必须是整数", {"key": key})

        entries: list[tuple[float, str, dict[str, Any]]] = []
        for item in self._task_files():
            if not _matches_filters(item.payload, raw_filters):
                continue
            entries.append((item.mtime, item.relative_path, _summary(item)))
        entries.sort(key=lambda entry: (-entry[0], entry[1]))
        selected = [summary for _, _, summary in entries[:limit]]
        return {"tasks": selected, "count": len(selected)}

    # -- preview -------------------------------------------------------------

    def preview(self, draft: Any) -> dict[str, Any]:
        """PreviewResult: ``{target_path, revision, unified_diff, stats, fingerprint}``."""

        payload = normalize_draft(draft)
        task_id = self._require_task_id(payload)
        target = self.target_for_task_id(task_id)
        relative_path = self.relative(target)
        base_revision = revision_of_file(target)
        old_text = ""
        existing = self._read_bytes(target)
        if existing is not None:
            try:
                old_text = existing.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WriterError(
                    E_IO,
                    f"目标文件不是 UTF-8：{exc}",
                    {"target_path": relative_path},
                ) from exc
        new_text = render_task(payload)
        diff = _unified_diff(old_text, new_text, relative_path)
        return {
            "target_path": relative_path,
            "revision": base_revision,
            "unified_diff": diff,
            "stats": _diff_stats(diff),
            "fingerprint": fingerprint_for(payload, relative_path, base_revision),
        }

    # -- commit --------------------------------------------------------------

    def commit(
        self,
        draft: Any,
        *,
        fingerprint: str,
        expected_revision: str,
        author: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Commit a previewed draft (fingerprint + revision + forced re-check).

        Returns a CommitResult dict with ``status`` ``committed`` or ``rejected``.
        Refusals raise :class:`WriterError` (``E_STALE_REVISION`` /
        ``E_FINGERPRINT_MISMATCH`` / ``E_PATH_DENIED`` / ...).
        """

        payload = normalize_draft(draft)
        task_id = self._require_task_id(payload)
        fingerprint = self._require_text(fingerprint, "fingerprint")
        expected_revision = self._require_text(expected_revision, "expected_revision")
        author = self._require_text(author, "author")
        if not isinstance(note, str):
            raise WriterError(
                E_INVALID_ARGUMENT,
                "note 必须是字符串",
                {"type": type(note).__name__},
            )
        target = self.target_for_task_id(task_id)
        relative_path = self.relative(target)

        with self.lock(task_id):
            current = self._read_bytes(target)
            actual = revision_of_bytes(current) if current is not None else EMPTY_REVISION
            if expected_revision != actual:
                raise WriterError(
                    E_STALE_REVISION,
                    "文件已被修改，拒绝覆盖",
                    {
                        "expected": expected_revision,
                        "actual": actual,
                        "target_path": relative_path,
                        "hint": "重新 preview_draft 后再确认",
                    },
                )
            computed = fingerprint_for(payload, relative_path, actual)
            if fingerprint != computed:
                raise WriterError(
                    E_FINGERPRINT_MISMATCH,
                    "指纹不匹配：确认的草案与提交的草案不一致",
                    {
                        "expected": fingerprint,
                        "actual": computed,
                        "base_revision": actual,
                        "target_path": relative_path,
                    },
                )
            report = self.validate(payload, target=target)
            if report.has_errors:
                return {"status": "rejected", "report": report.to_dict()}

            rendered = render_task(payload).encode("utf-8")
            commit_id = uuid4().hex[:12]
            backup = self._backup(target, task_id, commit_id) if current is not None else None
            _atomic_write(target, rendered)
            new_revision = revision_of_bytes(rendered)
            old_text = (
                current.decode("utf-8", errors="replace") if current is not None else ""
            )
            diff = _unified_diff(old_text, rendered.decode("utf-8"), relative_path)
            audit = self._audit_record(
                event="commit",
                status="committed",
                commit_id=commit_id,
                task_id=task_id,
                target_path=relative_path,
                author=author,
                note=note,
                base_revision=actual,
                new_revision=new_revision,
                backup_path=self.relative(backup) if backup is not None else None,
                diff_stats=_diff_stats(diff),
            )
            audit_error = self._audit_safely(audit)
            result: dict[str, Any] = {
                "status": "committed",
                "commit_id": commit_id,
                "backup_path": self.relative(backup) if backup is not None else None,
                "new_revision": new_revision,
                "report": report.to_dict(),
            }
            if audit_error is not None:
                result["audit_error"] = audit_error
            return result

    # -- rollback ------------------------------------------------------------

    def rollback(self, commit_id: str) -> dict[str, Any]:
        """Restore the pre-commit backup of ``commit_id`` as a new commit.

        History is append-only: the restored content gets a fresh ``commit_id``
        and the current content becomes the new backup, so the rollback itself
        can be rolled back.
        """

        if not isinstance(commit_id, str) or not _COMMIT_ID_RE.fullmatch(commit_id):
            raise WriterError(
                E_INVALID_ARGUMENT,
                f"commit_id 非法：{commit_id!r}",
                {"commit_id": repr(commit_id)},
            )
        record = self._find_commit(commit_id)
        backup_path = self._resolve_backup(record.get("backup_path"))
        data = self._read_bytes(backup_path)
        if data is None:  # pragma: no cover - _resolve_backup already checks
            raise WriterError(
                E_BACKUP_NOT_FOUND,
                f"备份文件已不存在：{record.get('backup_path')}",
                {"commit_id": commit_id},
            )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WriterError(
                E_IO,
                f"备份文件不可解析：{exc}",
                {"commit_id": commit_id, "backup_path": str(record.get("backup_path"))},
            ) from exc
        if not isinstance(payload, Mapping):
            raise WriterError(
                E_IO,
                "备份文件顶层必须是 JSON 对象",
                {"commit_id": commit_id},
            )
        task_id = payload.get("task_id") or record.get("task_id")
        if not isinstance(task_id, str):
            raise WriterError(
                E_BACKUP_NOT_FOUND,
                "备份记录缺少 task_id，无法确定恢复目标",
                {"commit_id": commit_id},
            )
        target = self.target_for_task_id(task_id)
        relative_path = self.relative(target)

        with self.lock(task_id):
            report = self.validate(payload, target=target)
            if report.has_errors:
                return {"status": "rejected", "report": report.to_dict()}
            current = self._read_bytes(target)
            base_revision = revision_of_bytes(current) if current is not None else EMPTY_REVISION
            new_commit_id = uuid4().hex[:12]
            backup = (
                self._backup(target, task_id, new_commit_id)
                if current is not None
                else None
            )
            _atomic_write(target, data)
            new_revision = revision_of_bytes(data)
            audit = self._audit_record(
                event="rollback",
                status="committed",
                commit_id=new_commit_id,
                task_id=task_id,
                target_path=relative_path,
                author="rollback",
                note=f"restore {commit_id}",
                base_revision=base_revision,
                new_revision=new_revision,
                backup_path=self.relative(backup) if backup is not None else None,
                restored_from=commit_id,
                diff_stats={"added": 0, "removed": 0},
            )
            audit_error = self._audit_safely(audit)
            result: dict[str, Any] = {
                "status": "committed",
                "commit_id": new_commit_id,
                "backup_path": self.relative(backup) if backup is not None else None,
                "new_revision": new_revision,
                "restored_from": commit_id,
                "report": report.to_dict(),
            }
            if audit_error is not None:
                result["audit_error"] = audit_error
            return result

    # -- maintenance ---------------------------------------------------------

    def prepare(self) -> dict[str, Any]:
        """Write-mode startup: create dirs, clean temp files, reconcile audit."""

        for directory in (self.tasks_dir, self.backups_dir, self.locks_dir, self.audit_path.parent):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WriterError(
                    E_IO,
                    f"无法创建运行目录：{directory}（{exc}）",
                    {"path": str(directory)},
                ) from exc
        cleaned = self.cleanup_temp_files()
        recovered = self.reconcile_audit()
        return {
            "cleaned_temp_files": cleaned,
            "recovered_commits": recovered,
            "audit_path": self.relative(self.audit_path),
        }

    def cleanup_temp_files(self, *, grace_s: float | None = None) -> int:
        """Remove ``*.tmp-*`` residue from dead writers or older than the grace."""

        grace = self.temp_grace_s if grace_s is None else max(0.0, float(grace_s))
        removed = 0
        now = time.time()
        for directory in (self.tasks_dir, self.backups_dir):
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                name = path.name
                if ".tmp-" not in name:
                    continue
                try:
                    age = now - path.stat().st_mtime
                except OSError:  # pragma: no cover - raced with another cleaner
                    continue
                pid = _pid_from_temp_name(name)
                dead_owner = pid is not None and not _pid_alive(pid)
                if dead_owner or age >= grace:
                    _try_unlink(path)
                    removed += 1
        return removed

    def reconcile_audit(self) -> int:
        """Append a ``recovered`` record when disk moved without an audit tail."""

        latest: dict[str, dict[str, Any]] = {}
        for record in self.read_audit():
            task_id = record.get("task_id")
            if isinstance(task_id, str):
                latest[task_id] = record
        recovered = 0
        for task_id, record in latest.items():
            try:
                target = self.target_for_task_id(task_id)
            except WriterError:
                continue
            if not target.is_file():
                continue
            actual = revision_of_file(target)
            if actual == record.get("new_revision"):
                continue
            audit = self._audit_record(
                event="recovered",
                status="recovered",
                commit_id=uuid4().hex[:12],
                task_id=task_id,
                target_path=self.relative(target),
                author="recovery",
                note="revision on disk does not match the audit tail",
                base_revision=record.get("new_revision"),
                new_revision=actual,
                backup_path=None,
                diff_stats={"added": 0, "removed": 0},
                recovered_from=record.get("commit_id"),
            )
            if self._audit_safely(audit) is None:
                recovered += 1
        return recovered

    def read_audit(self) -> list[dict[str, Any]]:
        """Read ``traces/commits.jsonl`` (tolerates a corrupt trailing line)."""

        if not self.audit_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            with self.audit_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        value = json.loads(stripped)
                    except json.JSONDecodeError:
                        LOGGER.warning(
                            "Skipping malformed commit audit line in %s",
                            self.audit_path,
                        )
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError as exc:
            raise WriterError(
                E_IO,
                f"读取审计日志失败：{exc}",
                {"audit_path": self.relative(self.audit_path)},
            ) from exc
        return records

    # -- internals -----------------------------------------------------------

    def _read_bytes(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WriterError(
                E_IO,
                f"读取失败：{exc}",
                {"path": self.relative(path)},
            ) from exc

    def _require_task_id(self, payload: Mapping[str, Any]) -> str:
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise WriterError(
                E_INVALID_ARGUMENT,
                "草案缺少 task_id，无法确定目标文件",
                {"field": "task_id"},
            )
        if not TASK_ID_RE.fullmatch(task_id):
            raise WriterError(
                E_PATH_DENIED,
                f"task_id 非法，拒绝构造写入路径：{task_id!r}",
                {"task_id": task_id},
            )
        return task_id

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise WriterError(
                E_INVALID_ARGUMENT,
                f"缺少必填参数 {field}",
                {"field": field},
            )
        return value.strip()

    def _backup(self, target: Path, task_id: str, commit_id: str) -> Path:
        data = self._read_bytes(target)
        if data is None:  # pragma: no cover - caller checks existence first
            raise WriterError(E_IO, "备份源文件不存在", {"target_path": self.relative(target)})
        directory = self.backups_dir / task_id
        destination = directory / f"{_utc_stamp()}-{commit_id}.json"
        _atomic_write(destination, data)
        return destination

    def _audit_record(self, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        record.update(fields)
        return record

    def _audit_safely(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            self._write_audit(record)
        except WriterError as exc:
            LOGGER.error("Commit audit append failed: %s", exc)
            return exc.to_envelope()["error"]
        return None

    def _write_audit(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with self._audit_lock:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    self.audit_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | O_BINARY,
                    0o644,
                )
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            raise WriterError(
                E_AUDIT,
                f"追加审计日志失败：{exc}",
                {"audit_path": self.relative(self.audit_path)},
            ) from exc

    def _find_commit(self, commit_id: str) -> dict[str, Any]:
        for record in reversed(self.read_audit()):
            if (
                record.get("commit_id") == commit_id
                and record.get("status") == "committed"
                and isinstance(record.get("backup_path"), str)
            ):
                return record
        raise WriterError(
            E_BACKUP_NOT_FOUND,
            f"未找到可回滚的提交 {commit_id}（需要该提交的备份记录）",
            {
                "commit_id": commit_id,
                "audit_path": self.relative(self.audit_path),
                "hint": "只有产生过备份的提交才能回滚；新建文件的首次提交没有备份",
            },
        )

    def _resolve_backup(self, backup_path: Any) -> Path:
        if not isinstance(backup_path, str) or not backup_path.strip():
            raise WriterError(
                E_BACKUP_NOT_FOUND,
                "提交记录缺少备份路径，无法回滚",
                {"backup_path": repr(backup_path)},
            )
        raw = self.root / backup_path
        if raw.is_symlink():
            raise WriterError(
                E_PATH_DENIED,
                f"拒绝符号链接备份：{backup_path}",
                {"backup_path": backup_path},
            )
        candidate = raw.resolve()
        root = self.backups_dir.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise WriterError(
                E_PATH_DENIED,
                f"备份路径越权：{backup_path}",
                {"backup_path": backup_path, "root": str(root)},
            ) from exc
        if candidate.suffix.lower() != ".json" or not candidate.is_file():
            raise WriterError(
                E_BACKUP_NOT_FOUND,
                f"备份文件不存在：{backup_path}",
                {"backup_path": backup_path},
            )
        return candidate


def _summary(item: _TaskFile) -> dict[str, Any]:
    payload = item.payload
    level_range = payload.get("level_range")
    level_min = level_max = None
    if isinstance(level_range, Mapping):
        raw_min = level_range.get("min")
        raw_max = level_range.get("max")
        level_min = raw_min if isinstance(raw_min, int) and not isinstance(raw_min, bool) else None
        level_max = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else None
    giver = payload.get("giver")
    npc_id = giver.get("npc_id") if isinstance(giver, Mapping) else None
    tags = payload.get("tags")
    return {
        "task_id": payload.get("task_id"),
        "title": payload.get("title"),
        "task_type": payload.get("task_type"),
        "level_min": level_min,
        "level_max": level_max,
        "giver_npc_id": npc_id if isinstance(npc_id, str) else None,
        "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
        "path": item.relative_path,
        "revision": item.revision,
        "updated_at": datetime.fromtimestamp(item.mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


def _matches_filters(payload: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    task_type = filters.get("task_type")
    if task_type is not None and payload.get("task_type") != task_type:
        return False
    tag = filters.get("tag")
    if tag is not None:
        tags = payload.get("tags")
        if not isinstance(tags, list) or tag not in tags:
            return False
    level_range = payload.get("level_range")
    minimum = level_range.get("min") if isinstance(level_range, Mapping) else None
    maximum = level_range.get("max") if isinstance(level_range, Mapping) else None
    level_min_filter = filters.get("level_min")
    if level_min_filter is not None and (
        not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < level_min_filter
    ):
        return False
    level_max_filter = filters.get("level_max")
    if level_max_filter is not None and (
        not isinstance(minimum, int) or isinstance(minimum, bool) or minimum > level_max_filter
    ):
        return False
    keyword = filters.get("keyword")
    if keyword is not None:
        needle = keyword.casefold()
        haystack = f"{payload.get('task_id', '')} {payload.get('title', '')}".casefold()
        if needle not in haystack:
            return False
    return True
