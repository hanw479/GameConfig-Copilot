"""Layered memory: sessions (S6-01) and designer preferences (S6-02/03).

``docs/02-architecture.md`` §8 defines three keys; this package owns the two
that are not task-context snapshots:

======================  ======================================================
key                     module
======================  ======================================================
``sess:{id}:state``     :mod:`gameconfig_copilot.memory.session`
``pref:{designer_id}``  :mod:`gameconfig_copilot.memory.preference`
======================  ======================================================

Both stores are process-restart safe, clock-injectable (TTL tests never sleep)
and free of LangGraph/LLM dependencies. The Agent layer reads the frozen
:class:`~gameconfig_copilot.memory.preference.Preference` interface and injects
``preference.defaults()`` or ``preference.value_for(...)``; explicit user
instructions always win (:meth:`Preference.value_for`).

Owner: Memory Worker Mem (``docs/06-multi-agent-collaboration.md`` §5.2).
"""

from __future__ import annotations

from .preference import (
    ANOMALY_SIGMA,
    DEFAULT_ALPHA,
    DEFAULT_PREFERENCE_TTL_S,
    CommitEvent,
    ExtractionResult,
    JsonPreferenceStore,
    Preference,
    PreferenceBackendUnavailable,
    PreferenceError,
    PreferenceStore,
    PreferenceStoreError,
    PreferenceValidationError,
    RedisPreferenceStore,
    extract,
    open_preference_store,
)
from .session import (
    DEFAULT_SESSION_TTL_S,
    MemoryMessage,
    SessionExpiredError,
    SessionMemoryError,
    SessionNotFoundError,
    SessionState,
    SessionStore,
    SessionValueError,
)

__all__ = [
    "ANOMALY_SIGMA",
    "DEFAULT_ALPHA",
    "DEFAULT_PREFERENCE_TTL_S",
    "DEFAULT_SESSION_TTL_S",
    "CommitEvent",
    "ExtractionResult",
    "JsonPreferenceStore",
    "MemoryMessage",
    "Preference",
    "PreferenceBackendUnavailable",
    "PreferenceError",
    "PreferenceStore",
    "PreferenceStoreError",
    "PreferenceValidationError",
    "RedisPreferenceStore",
    "SessionExpiredError",
    "SessionMemoryError",
    "SessionNotFoundError",
    "SessionState",
    "SessionStore",
    "SessionValueError",
    "extract",
    "open_preference_store",
]
