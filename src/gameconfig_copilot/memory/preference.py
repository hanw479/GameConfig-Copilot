"""Designer preference memory: storage (S6-02) and extraction (S6-03).

Owner: Memory Worker Mem (``docs/06-multi-agent-collaboration.md`` §5.2). This
module has no LangGraph/LLM dependency and never imports ``agents``/``api``.

Model (``docs/02-architecture.md`` §8)
--------------------------------------
Four structured preference fields are learned from commit events:

=====================  =========  ============================================
field                  kind       learned from
=====================  =========  ============================================
``default_reward_band`` numeric   sum of top-level ``gold`` rewards in the
                                  final committed draft
``task_types``          weighted  ``draft.task_type``
``strictness``          ordinal   ``signals.strictness`` (low / medium / high)
``naming_style``        weighted  ``signals.naming_style``
=====================  =========  ============================================

Numeric/ordinal fields use a sliding average with ``alpha = 0.3``. A value
further than ``2 sigma`` from the current mean is **not** applied: it is
appended to the pending-confirmation queue instead. Every applied change
records ``{field, old, new, source_commit, updated_at}`` so ``show`` can explain
where a preference came from. An explicit instruction (``explicit_fields`` /
``explicit`` / an edit marked ``explicit``) never updates the preference and
wins over it via :meth:`Preference.value_for`.

Storage
-------
:class:`JsonPreferenceStore` is the default backend (one JSON file per designer,
atomic replace, cross-process lock). :class:`RedisPreferenceStore` is the
pluggable adapter: inject a redis-py-like client and the same logic runs against
Redis; without a client every operation raises
:class:`PreferenceBackendUnavailable` instead of silently falling back.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

__all__ = [
    "ANOMALY_SIGMA",
    "DEFAULT_ALPHA",
    "DEFAULT_PREFERENCE_TTL_S",
    "CommitEvent",
    "ExtractionResult",
    "JsonPreferenceStore",
    "Preference",
    "PreferenceBackendUnavailable",
    "PreferenceError",
    "PreferenceStore",
    "PreferenceStoreError",
    "PreferenceValidationError",
    "RedisPreferenceStore",
    "extract",
    "open_preference_store",
]

DEFAULT_ALPHA = 0.3
"""``docs/02`` §8: sliding average weight for one observation."""

ANOMALY_SIGMA = 2.0
"""Deviation (in current sigmas) that sends a sample to the pending queue."""

DEFAULT_PREFERENCE_TTL_S = 90 * 24 * 60 * 60
"""``docs/02`` §8: ``pref:{designer_id}`` lives for 90 days."""

PREFERENCE_FIELDS = (
    "default_reward_band",
    "task_types",
    "strictness",
    "naming_style",
)
STRICTNESS_LEVELS = {"low": 0.0, "medium": 0.5, "high": 1.0}
MAX_SOURCES = 100
MAX_PENDING = 50
MIN_WEIGHT = 0.01
TOP_VALUES = 3
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PREFERENCE_ROOT = REPO_ROOT / "data" / "memory" / "preferences"
LOCK_STALE_AFTER_S = 30.0
O_BINARY = getattr(os, "O_BINARY", 0)

_FIELD_ALIASES = {
    "default_reward_band": "default_reward_band",
    "reward_band": "default_reward_band",
    "reward_band_side": "default_reward_band",
    "reward": "default_reward_band",
    "rewards": "default_reward_band",
    "task_types": "task_types",
    "task_type": "task_types",
    "strictness": "strictness",
    "naming_style": "naming_style",
    "naming": "naming_style",
}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NAMING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat()


def _parse(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _canonical_field(raw: Any) -> str | None:
    """Map a draft path / preference name to one of ``PREFERENCE_FIELDS``."""

    if not isinstance(raw, str):
        return None
    text = raw.strip()
    lowered = text.lower()
    if lowered in _FIELD_ALIASES:
        return _FIELD_ALIASES[lowered]
    if lowered.startswith("rewards") or lowered.startswith("reward"):
        return "default_reward_band"
    if lowered.startswith("task_type"):
        return "task_types"
    if lowered.startswith("strictness"):
        return "strictness"
    if lowered.startswith("naming"):
        return "naming_style"
    return None


class PreferenceError(RuntimeError):
    """Base class for preference-memory failures."""


class PreferenceValidationError(PreferenceError, ValueError):
    """A commit event / preference value violates the frozen contract."""


class PreferenceStoreError(PreferenceError):
    """Storage-layer failure (corrupt file, lock timeout, ...)."""


class PreferenceBackendUnavailable(PreferenceStoreError):
    """The configured backend cannot be used (e.g. Redis client not injected)."""


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


def _band(mean: float, sigma: float) -> dict[str, int]:
    return {
        "min": max(0, int(math.floor(mean - ANOMALY_SIGMA * sigma))),
        "max": int(math.ceil(mean + ANOMALY_SIGMA * sigma)),
    }


def _strictness_label(mean: float) -> str:
    if mean < 0.34:
        return "low"
    if mean < 0.67:
        return "medium"
    return "high"


def _current_value(preference: Preference, field_name: str) -> Any:
    payload = preference.fields.get(field_name) or {}
    if field_name == "default_reward_band":
        band = payload.get("band")
        return dict(band) if isinstance(band, Mapping) else None
    if field_name == "task_types":
        return list(payload.get("top", []))
    if field_name == "strictness":
        return payload.get("label")
    if field_name == "naming_style":
        top = payload.get("top") or []
        return top[0] if top else None
    return None


@dataclass
class Preference:
    """Structured, explainable designer preferences."""

    designer_id: str
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    updated_at: datetime | None = None
    expired: bool = False

    @classmethod
    def empty(cls, designer_id: str, *, expired: bool = False) -> Preference:
        return cls(designer_id=designer_id, expired=expired)

    def defaults(self) -> dict[str, Any]:
        """Compact structured defaults for context injection (``docs/02`` §8)."""

        values: dict[str, Any] = {}
        reward = self.fields.get("default_reward_band")
        if reward and reward.get("count"):
            values["default_reward_band"] = dict(reward["band"])
        task_types = self.fields.get("task_types")
        if task_types and task_types.get("top"):
            values["task_types"] = list(task_types["top"])
        strictness = self.fields.get("strictness")
        if strictness and strictness.get("count"):
            values["strictness"] = strictness.get("label")
        naming = self.fields.get("naming_style")
        if naming and naming.get("top"):
            values["naming_style"] = naming["top"][0]
        return values

    def value_for(self, field_name: str, *, explicit: Any | None = None) -> Any:
        """Explicit instruction wins over the learned default (S6-03/S6-04).

        ``value_for("default_reward_band", explicit=500)`` returns ``500`` no
        matter what the preference band says; without ``explicit`` it returns
        the learned default (or ``None`` when nothing has been learned yet).
        """

        if field_name not in PREFERENCE_FIELDS:
            raise PreferenceValidationError(f"未知偏好字段：{field_name!r}")
        if explicit is not None:
            return explicit
        return self.defaults().get(field_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "designer_id": self.designer_id,
            "fields": self.fields,
            "sources": self.sources,
            "pending": self.pending,
            "updated_at": _iso(self.updated_at) if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Preference:
        fields_payload = payload.get("fields")
        if not isinstance(fields_payload, Mapping):
            fields_payload = {}
        sources_payload = payload.get("sources")
        if not isinstance(sources_payload, Sequence) or isinstance(
            sources_payload, (str, bytes)
        ):
            sources_payload = ()
        pending_payload = payload.get("pending")
        if not isinstance(pending_payload, Sequence) or isinstance(
            pending_payload, (str, bytes)
        ):
            pending_payload = ()
        updated = payload.get("updated_at")
        return cls(
            designer_id=str(payload.get("designer_id", "")),
            fields={
                str(key): dict(value)
                for key, value in fields_payload.items()
                if isinstance(value, Mapping)
            },
            sources=[
                dict(item) for item in sources_payload if isinstance(item, Mapping)
            ],
            pending=[
                dict(item) for item in pending_payload if isinstance(item, Mapping)
            ],
            updated_at=_parse(str(updated)) if updated else None,
        )


@dataclass(frozen=True)
class CommitEvent:
    """Input of :func:`extract`: the *final* committed draft plus edit evidence.

    ``edits`` are the human modification points relative to the model draft.
    They are recorded in :class:`ExtractionResult` and used to flag explicit
    instructions (``explicit=True``); observations themselves always come from
    the final draft, so an edit is not counted twice.
    """

    designer_id: str
    commit_id: str
    draft: Mapping[str, Any]
    task_id: str = ""
    session_id: str = ""
    edits: tuple[dict[str, Any], ...] = ()
    explicit_fields: tuple[str, ...] = ()
    explicit_values: Mapping[str, Any] = field(default_factory=dict)
    signals: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_input(cls, value: CommitEvent | Mapping[str, Any]) -> CommitEvent:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise PreferenceValidationError(
                f"提交事件必须是映射或 CommitEvent：{type(value).__name__}"
            )
        raw_draft = value.get("draft", value.get("final_draft"))
        if raw_draft is None:
            raise PreferenceValidationError("提交事件缺少 draft（草案终值）")
        dump = getattr(raw_draft, "model_dump", None)
        if callable(dump):
            raw_draft = dump(mode="json")
        if not isinstance(raw_draft, Mapping):
            raise PreferenceValidationError(
                f"draft 必须是映射或 TaskDraft：{type(raw_draft).__name__}"
            )
        designer_id = value.get("designer_id", value.get("designer", ""))
        if not isinstance(designer_id, str) or not designer_id.strip():
            raise PreferenceValidationError("提交事件缺少 designer_id")
        commit_id = value.get("commit_id", value.get("source_commit", ""))
        if not isinstance(commit_id, str) or not commit_id.strip():
            raise PreferenceValidationError(
                "提交事件缺少 commit_id：偏好来源必须能指回提交"
            )

        explicit_fields: list[str] = []
        raw_fields = value.get("explicit_fields") or ()
        if isinstance(raw_fields, str):
            explicit_fields.append(raw_fields)
        elif isinstance(raw_fields, Sequence):
            explicit_fields.extend(str(item) for item in raw_fields)
        explicit_values: dict[str, Any] = {}
        raw_explicit = value.get("explicit") or {}
        if isinstance(raw_explicit, Mapping):
            for key, item in raw_explicit.items():
                explicit_fields.append(str(key))
                explicit_values[str(key)] = item
        elif isinstance(raw_explicit, Sequence) and not isinstance(raw_explicit, str):
            explicit_fields.extend(str(item) for item in raw_explicit)

        raw_edits = (
            value.get("edits")
            or value.get("human_edits")
            or value.get("modifications")
            or ()
        )
        edits: list[dict[str, Any]] = []
        for item in raw_edits:
            if isinstance(item, str):
                edits.append({"field": item})
            elif isinstance(item, Mapping):
                edit = dict(item)
                if "field" not in edit and "path" in edit:
                    edit["field"] = edit["path"]
                edits.append(edit)
            else:
                raise PreferenceValidationError(
                    f"人工修改点格式非法：{type(item).__name__}"
                )
        for edit in edits:
            if edit.get("explicit") is True:
                explicit_fields.append(str(edit.get("field", "")))

        signals = value.get("signals") or {}
        if not isinstance(signals, Mapping):
            raise PreferenceValidationError("signals 必须是映射")
        merged_signals = dict(signals)
        for key in ("strictness", "naming_style"):
            if key in value and key not in merged_signals:
                merged_signals[key] = value[key]

        task_id = value.get("task_id", raw_draft.get("task_id", ""))
        session_id = value.get("session_id", "")
        return cls(
            designer_id=designer_id.strip(),
            commit_id=commit_id.strip(),
            draft=dict(raw_draft),
            task_id=str(task_id) if task_id else "",
            session_id=str(session_id) if session_id else "",
            edits=tuple(edits),
            explicit_fields=tuple(field for field in explicit_fields if field),
            explicit_values=explicit_values,
            signals=merged_signals,
        )


@dataclass
class ExtractionResult:
    """What one commit event changed (and what it refused to change)."""

    designer_id: str
    updated: list[dict[str, Any]]
    skipped_explicit: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    human_edits: list[dict[str, Any]]
    preference: Preference

    def to_dict(self) -> dict[str, Any]:
        return {
            "designer_id": self.designer_id,
            "updated": self.updated,
            "skipped_explicit": self.skipped_explicit,
            "anomalies": self.anomalies,
            "human_edits": self.human_edits,
            "preference": self.preference.to_dict(),
        }


# ---------------------------------------------------------------------------
# Extraction (S6-03)
# ---------------------------------------------------------------------------


def _gold_total(draft: Mapping[str, Any]) -> int | None:
    rewards = draft.get("rewards")
    if not isinstance(rewards, Sequence) or isinstance(rewards, (str, bytes)):
        return None
    total = 0
    seen = False
    for reward in rewards:
        if not isinstance(reward, Mapping):
            continue
        if str(reward.get("type", "")).lower() != "gold":
            continue
        amount = reward.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            continue
        if amount < 0:
            raise PreferenceValidationError(f"gold 奖励不能为负：{amount}")
        total += int(amount)
        seen = True
    if not seen or total <= 0:
        return None
    return total


def _coerce_strictness(value: Any) -> float:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in STRICTNESS_LEVELS:
            return STRICTNESS_LEVELS[key]
        raise PreferenceValidationError(
            f"strictness 只接受 {sorted(STRICTNESS_LEVELS)} 或 0..1：{value!r}"
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreferenceValidationError(f"strictness 必须是标签或数值：{value!r}")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise PreferenceValidationError(f"strictness 数值必须在 0..1：{numeric}")
    return numeric


def _coerce_naming_style(value: Any) -> str:
    if not isinstance(value, str) or not _NAMING_RE.fullmatch(value.strip()):
        raise PreferenceValidationError(f"naming_style 非法：{value!r}")
    return value.strip()


def _coerce_task_type(value: Any) -> str:
    if not isinstance(value, str) or not _NAMING_RE.fullmatch(value.strip()):
        raise PreferenceValidationError(f"task_type 非法：{value!r}")
    return value.strip()


def _observations(event: CommitEvent) -> dict[str, Any]:
    observations: dict[str, Any] = {}
    gold = _gold_total(event.draft)
    if gold is not None:
        observations["default_reward_band"] = gold
    task_type = event.draft.get("task_type")
    if task_type is not None:
        observations["task_types"] = _coerce_task_type(task_type)
    if "strictness" in event.signals:
        observations["strictness"] = _coerce_strictness(event.signals["strictness"])
    if "naming_style" in event.signals:
        observations["naming_style"] = _coerce_naming_style(
            event.signals["naming_style"]
        )
    return observations


def _prior_sigma(field_name: str, value: float) -> float:
    if field_name == "strictness":
        return 0.25
    return max(abs(value) * 0.25, 1.0)


def _pending_entry(
    *,
    field_name: str,
    value: float,
    mean: float,
    sigma: float,
    commit_id: str,
    task_id: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "id": f"pnd_{uuid4().hex[:12]}",
        "field": field_name,
        "value": value,
        "mean": round(mean, 6),
        "sigma": round(sigma, 6),
        "threshold": round(ANOMALY_SIGMA * sigma, 6),
        "source_commit": commit_id,
        "task_id": task_id,
        "observed_at": _iso(now),
        "reason": f"|value - mean| > {ANOMALY_SIGMA:g} sigma，未更新偏好",
    }


def _enqueue(preference: Preference, entry: dict[str, Any]) -> bool:
    for existing in preference.pending:
        if (
            existing.get("field") == entry["field"]
            and existing.get("source_commit") == entry["source_commit"]
            and existing.get("value") == entry["value"]
        ):
            return False
    preference.pending.append(entry)
    del preference.pending[:-MAX_PENDING]
    return True


def _update_numeric(
    preference: Preference,
    field_name: str,
    value: float,
    *,
    event: CommitEvent,
    now: datetime,
    alpha: float,
    anomaly_sigma: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = preference.fields.get(field_name)
    old_value = _current_value(preference, field_name)
    if payload is None or int(payload.get("count", 0)) == 0:
        mean = float(value)
        sigma = _prior_sigma(field_name, mean)
        count = 1
    else:
        mean = float(payload.get("mean", 0.0))
        sigma = float(payload.get("sigma", 0.0))
        count = int(payload.get("count", 0))
        deviation = abs(value - mean)
        if deviation > anomaly_sigma * sigma:
            return None, _pending_entry(
                field_name=field_name,
                value=value,
                mean=mean,
                sigma=sigma,
                commit_id=event.commit_id,
                task_id=event.task_id,
                now=now,
            )
        new_mean = (1.0 - alpha) * mean + alpha * value
        new_variance = (1.0 - alpha) * sigma**2 + alpha * (value - mean) ** 2
        mean = new_mean
        sigma = math.sqrt(max(0.0, new_variance))
        count += 1

    updated_payload: dict[str, Any] = {
        "kind": "numeric" if field_name == "default_reward_band" else "ordinal",
        "mean": round(mean, 6),
        "sigma": round(sigma, 6),
        "count": count,
        "updated_at": _iso(now),
        "source_commit": event.commit_id,
    }
    if field_name == "default_reward_band":
        updated_payload["band"] = _band(mean, sigma)
    else:
        updated_payload["label"] = _strictness_label(mean)
    preference.fields[field_name] = updated_payload
    return (
        {
            "field": field_name,
            "old": old_value,
            "new": _current_value(preference, field_name),
            "source_commit": event.commit_id,
            "updated_at": _iso(now),
        },
        None,
    )


def _update_weighted(
    preference: Preference,
    field_name: str,
    value: str,
    *,
    event: CommitEvent,
    now: datetime,
    alpha: float,
) -> dict[str, Any]:
    payload = preference.fields.get(field_name) or {}
    old_value = _current_value(preference, field_name)
    weights = {
        str(key): float(weight)
        for key, weight in (payload.get("weights") or {}).items()
    }
    weights = {key: weight * (1.0 - alpha) for key, weight in weights.items()}
    weights[value] = weights.get(value, 0.0) + alpha
    weights = {key: weight for key, weight in weights.items() if weight >= MIN_WEIGHT}
    total = sum(weights.values())
    if total > 0:
        weights = {key: round(weight / total, 6) for key, weight in weights.items()}
    top = [
        key
        for key, _ in sorted(weights.items(), key=lambda item: (-item[1], item[0]))[
            :TOP_VALUES
        ]
    ]
    preference.fields[field_name] = {
        "kind": "weighted",
        "weights": weights,
        "top": top,
        "count": int(payload.get("count", 0)) + 1,
        "updated_at": _iso(now),
        "source_commit": event.commit_id,
    }
    return {
        "field": field_name,
        "old": old_value,
        "new": _current_value(preference, field_name),
        "source_commit": event.commit_id,
        "updated_at": _iso(now),
    }


def _apply_event(
    preference: Preference,
    event: CommitEvent,
    *,
    now: datetime,
    alpha: float,
    anomaly_sigma: float,
) -> ExtractionResult:
    observations = _observations(event)
    explicit: dict[str, Any] = {}
    for raw in event.explicit_fields:
        canonical = _canonical_field(raw)
        if canonical is not None:
            explicit.setdefault(canonical, None)
    for raw_key, raw_value in event.explicit_values.items():
        canonical = _canonical_field(raw_key)
        if canonical is not None:
            explicit[canonical] = raw_value

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for field_name, value in observations.items():
        if field_name in explicit:
            skipped.append(
                {
                    "field": field_name,
                    "value": explicit[field_name]
                    if explicit[field_name] is not None
                    else value,
                    "source_commit": event.commit_id,
                    "reason": "显式指令优先，不更新偏好",
                }
            )
            continue
        if field_name in ("default_reward_band", "strictness"):
            source, anomaly = _update_numeric(
                preference,
                field_name,
                float(value),
                event=event,
                now=now,
                alpha=alpha,
                anomaly_sigma=anomaly_sigma,
            )
        else:
            source, anomaly = (
                _update_weighted(
                    preference,
                    field_name,
                    str(value),
                    event=event,
                    now=now,
                    alpha=alpha,
                ),
                None,
            )
        if anomaly is not None:
            _enqueue(preference, anomaly)
            anomalies.append(anomaly)
        if source is not None:
            updated.append(source)

    if updated or anomalies:
        preference.updated_at = now
        preference.sources.extend(updated)
        del preference.sources[:-MAX_SOURCES]
    return ExtractionResult(
        designer_id=preference.designer_id,
        updated=updated,
        skipped_explicit=skipped,
        anomalies=anomalies,
        human_edits=[dict(edit) for edit in event.edits],
        preference=preference,
    )


def _force_update(
    preference: Preference,
    field_name: str,
    value: Any,
    *,
    source_commit: str,
    now: datetime,
    alpha: float,
) -> dict[str, Any]:
    """Apply a pending sample after a human confirmed it (even if anomalous)."""

    old_value = _current_value(preference, field_name)
    payload = preference.fields.get(field_name)
    if field_name in ("default_reward_band", "strictness"):
        numeric = _coerce_strictness(value) if field_name == "strictness" else float(value)
        if field_name == "default_reward_band" and numeric < 0:
            raise PreferenceValidationError("奖励数值不能为负")
        if payload is None or int(payload.get("count", 0)) == 0:
            mean = numeric
            sigma = _prior_sigma(field_name, numeric)
            count = 1
        else:
            mean = float(payload.get("mean", 0.0))
            sigma = float(payload.get("sigma", 0.0))
            count = int(payload.get("count", 0))
            new_mean = (1.0 - alpha) * mean + alpha * numeric
            new_variance = (
                (1.0 - alpha) * sigma**2 + alpha * (numeric - mean) ** 2
            )
            mean = new_mean
            sigma = math.sqrt(max(0.0, new_variance))
            count += 1
        new_payload: dict[str, Any] = {
            "kind": "numeric" if field_name == "default_reward_band" else "ordinal",
            "mean": round(mean, 6),
            "sigma": round(sigma, 6),
            "count": count,
            "updated_at": _iso(now),
            "source_commit": source_commit,
        }
        if field_name == "default_reward_band":
            new_payload["band"] = _band(mean, sigma)
        else:
            new_payload["label"] = _strictness_label(mean)
        preference.fields[field_name] = new_payload
    else:
        string_value = str(value)
        if field_name == "task_types":
            string_value = _coerce_task_type(string_value)
        else:
            string_value = _coerce_naming_style(string_value)
        source = _update_weighted(
            preference,
            field_name,
            string_value,
            event=CommitEvent(
                designer_id=preference.designer_id,
                commit_id=source_commit,
                draft={},
            ),
            now=now,
            alpha=alpha,
        )
        source["old"] = old_value
        preference.sources.append(source)
        preference.updated_at = now
        del preference.sources[:-MAX_SOURCES]
        return source
    source = {
        "field": field_name,
        "old": old_value,
        "new": _current_value(preference, field_name),
        "source_commit": source_commit,
        "updated_at": _iso(now),
    }
    preference.sources.append(source)
    preference.updated_at = now
    del preference.sources[:-MAX_SOURCES]
    return source


# ---------------------------------------------------------------------------
# Storage interface
# ---------------------------------------------------------------------------


class PreferenceStore(Protocol):
    """Storage-agnostic interface consumed by the Agent layer (S6-04)."""

    def get(self, designer_id: str) -> Preference:
        """Load a designer preference (empty when unknown/expired)."""

    def put(self, preference: Preference) -> Preference:
        """Persist a preference snapshot."""

    def show(self, designer_id: str) -> dict[str, Any]:
        """Readable view: defaults + sources + pending queue."""

    def clear(self, designer_id: str) -> bool:
        """Delete a designer preference; True when something was removed."""

    def pending(self, designer_id: str) -> list[dict[str, Any]]:
        """Anomalous samples waiting for human confirmation."""

    def resolve_pending(
        self, designer_id: str, pending_id: str, *, accept: bool
    ) -> Preference:
        """Confirm or discard one pending sample."""

    def extract(self, event: CommitEvent | Mapping[str, Any]) -> ExtractionResult:
        """Learn from one commit event (S6-03)."""


class _BasePreferenceStore:
    """Shared get/put/show/clear/extract logic; backends implement I/O hooks."""

    backend_name = "abstract"

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_s: float = DEFAULT_PREFERENCE_TTL_S,
        alpha: float = DEFAULT_ALPHA,
        anomaly_sigma: float = ANOMALY_SIGMA,
    ) -> None:
        if isinstance(ttl_s, bool) or ttl_s <= 0:
            raise ValueError("ttl_s 必须 > 0")
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha 必须在 (0, 1] 区间")
        if anomaly_sigma <= 0:
            raise ValueError("anomaly_sigma 必须 > 0")
        self.ttl_s = float(ttl_s)
        self.alpha = float(alpha)
        self.anomaly_sigma = float(anomaly_sigma)
        self._clock = clock or _utcnow

    def now(self) -> datetime:
        return _as_utc(self._clock())

    # -- backend hooks -------------------------------------------------------

    def _read_payload(self, designer_id: str) -> Mapping[str, Any] | None:
        raise NotImplementedError

    def _write_payload(self, designer_id: str, payload: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def _delete_payload(self, designer_id: str) -> bool:
        raise NotImplementedError

    def location(self, designer_id: str) -> str:
        """Human-readable storage location (file path or Redis key)."""

        raise NotImplementedError

    @contextmanager
    def _lock(self, designer_id: str) -> Iterator[None]:
        del designer_id
        yield

    # -- public API ----------------------------------------------------------

    def get(self, designer_id: str) -> Preference:
        designer = _require_designer(designer_id)
        preference = self._load(designer)
        if self._expired(preference):
            return Preference.empty(designer, expired=True)
        return preference

    def _load(self, designer_id: str) -> Preference:
        """Read one preference, normalising corrupt payloads to a store error."""

        payload = self._read_payload(designer_id)
        if payload is None:
            return Preference.empty(designer_id)
        try:
            preference = Preference.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise PreferenceStoreError(
                f"偏好数据非法（{designer_id}）：{exc}"
            ) from exc
        preference.designer_id = designer_id
        return preference

    def put(self, preference: Preference) -> Preference:
        if not isinstance(preference, Preference):
            raise PreferenceValidationError(
                f"preference 必须是 Preference：{type(preference).__name__}"
            )
        designer = _require_designer(preference.designer_id)
        if preference.updated_at is None:
            preference.updated_at = self.now()
        with self._lock(designer):
            self._write_payload(designer, preference.to_dict())
        return self.get(designer)

    def show(self, designer_id: str) -> dict[str, Any]:
        designer = _require_designer(designer_id)
        preference = self.get(designer)
        return {
            "designer_id": designer,
            "backend": self.backend_name,
            "location": self.location(designer),
            "defaults": preference.defaults(),
            "fields": preference.fields,
            "sources": preference.sources,
            "pending": preference.pending,
            "updated_at": _iso(preference.updated_at)
            if preference.updated_at
            else None,
            "expired": preference.expired,
        }

    def clear(self, designer_id: str) -> bool:
        designer = _require_designer(designer_id)
        with self._lock(designer):
            return self._delete_payload(designer)

    def pending(self, designer_id: str) -> list[dict[str, Any]]:
        return list(self.get(designer_id).pending)

    def resolve_pending(
        self, designer_id: str, pending_id: str, *, accept: bool
    ) -> Preference:
        designer = _require_designer(designer_id)
        if not isinstance(pending_id, str) or not pending_id.strip():
            raise PreferenceValidationError("pending_id 必须是非空字符串")
        with self._lock(designer):
            preference = self._load(designer)
            if self._expired(preference):
                preference = Preference.empty(designer)
            entry = next(
                (item for item in preference.pending if item.get("id") == pending_id),
                None,
            )
            if entry is None:
                raise PreferenceValidationError(
                    f"待确认样本不存在：{pending_id}"
                )
            preference.pending = [
                item for item in preference.pending if item.get("id") != pending_id
            ]
            if accept:
                _force_update(
                    preference,
                    str(entry.get("field", "")),
                    entry.get("value"),
                    source_commit=str(entry.get("source_commit", "pending")),
                    now=self.now(),
                    alpha=self.alpha,
                )
            preference.updated_at = self.now()
            self._write_payload(designer, preference.to_dict())
        return preference

    def extract(self, event: CommitEvent | Mapping[str, Any]) -> ExtractionResult:
        commit_event = CommitEvent.from_input(event)
        designer = _require_designer(commit_event.designer_id)
        with self._lock(designer):
            preference = self._load(designer)
            if self._expired(preference):
                preference = Preference.empty(designer)
            result = _apply_event(
                preference,
                commit_event,
                now=self.now(),
                alpha=self.alpha,
                anomaly_sigma=self.anomaly_sigma,
            )
            self._write_payload(designer, preference.to_dict())
        return result

    # -- internals -----------------------------------------------------------

    def _expired(self, preference: Preference) -> bool:
        if preference.updated_at is None:
            return False
        return self.now() - _as_utc(preference.updated_at) > timedelta(
            seconds=self.ttl_s
        )


def _require_designer(designer_id: Any) -> str:
    if not isinstance(designer_id, str) or not designer_id.strip():
        raise PreferenceValidationError("designer_id 必须是非空字符串")
    return designer_id.strip()


# ---------------------------------------------------------------------------
# Local JSON backend (default)
# ---------------------------------------------------------------------------


class _FileLock:
    """Small cross-process lock based on ``O_CREAT | O_EXCL``.

    Preference updates are read-modify-write, so the JSON backend must
    serialise them. A lock older than ``stale_after_s`` is treated as abandoned
    (crashed writer) and broken; the operation itself is milliseconds.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout_s: float = 5.0,
        poll_s: float = 0.005,
        stale_after_s: float = LOCK_STALE_AFTER_S,
    ) -> None:
        self.path = path
        self.timeout_s = max(0.05, float(timeout_s))
        self.poll_s = max(0.001, float(poll_s))
        self.stale_after_s = max(1.0, float(stale_after_s))
        self._token = uuid4().hex
        self._held = False

    def __enter__(self) -> _FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY,
                    0o644,
                )
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise PreferenceStoreError(
                        f"获取偏好锁超时（{self.timeout_s:.1f}s）：{self.path.name}"
                    ) from None
                time.sleep(self.poll_s)
                continue
            try:
                os.write(descriptor, self._token.encode("ascii"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._held = True
            return

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if self.path.read_text(encoding="ascii") == self._token:
                self.path.unlink(missing_ok=True)
        except OSError:
            return

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if age <= self.stale_after_s:
            return False
        self.path.unlink(missing_ok=True)
        return True


class JsonPreferenceStore(_BasePreferenceStore):
    """Default backend: one JSON file per designer, atomic writes, file lock."""

    backend_name = "json"

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_s: float = DEFAULT_PREFERENCE_TTL_S,
        alpha: float = DEFAULT_ALPHA,
        anomaly_sigma: float = ANOMALY_SIGMA,
        lock_timeout_s: float = 5.0,
    ) -> None:
        super().__init__(
            clock=clock, ttl_s=ttl_s, alpha=alpha, anomaly_sigma=anomaly_sigma
        )
        self.root = (
            Path(root).expanduser() if root is not None else DEFAULT_PREFERENCE_ROOT
        )
        self.lock_timeout_s = max(0.05, float(lock_timeout_s))

    # -- paths ---------------------------------------------------------------

    def path_for(self, designer_id: str) -> Path:
        designer = _require_designer(designer_id)
        if _SAFE_NAME_RE.fullmatch(designer):
            name = designer
        else:  # keep arbitrary ids from escaping the root
            name = "id-" + sha256(designer.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{name}.json"

    def location(self, designer_id: str) -> str:
        return str(self.path_for(designer_id))

    # -- hooks ---------------------------------------------------------------

    def _read_payload(self, designer_id: str) -> Mapping[str, Any] | None:
        path = self.path_for(designer_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PreferenceStoreError(f"读取偏好失败：{exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Fail closed: never overwrite a file we could not understand.
            raise PreferenceStoreError(f"偏好文件损坏：{path}（{exc}）") from exc
        if not isinstance(payload, Mapping):
            raise PreferenceStoreError(f"偏好文件顶层必须是 JSON 对象：{path}")
        return payload

    def _write_payload(self, designer_id: str, payload: Mapping[str, Any]) -> None:
        path = self.path_for(designer_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex[:8]}")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY,
                0o600,
            )
            try:
                os.write(descriptor, text.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PreferenceStoreError(f"写入偏好失败：{exc}") from exc

    def _delete_payload(self, designer_id: str) -> bool:
        path = self.path_for(designer_id)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise PreferenceStoreError(f"清除偏好失败：{exc}") from exc
        return True

    @contextmanager
    def _lock(self, designer_id: str) -> Iterator[None]:
        path = self.path_for(designer_id).with_suffix(".json.lock")
        with _FileLock(path, timeout_s=self.lock_timeout_s):
            yield


# ---------------------------------------------------------------------------
# Redis backend (pluggable stub)
# ---------------------------------------------------------------------------


class RedisPreferenceStore(_BasePreferenceStore):
    """Redis adapter with no hard dependency on ``redis``.

    Inject any redis-py-like client (``get`` / ``set`` / ``delete``). Without a
    client the backend fails closed with :class:`PreferenceBackendUnavailable`,
    which keeps the default deployment JSON-only and offline tests free of the
    ``redis`` wheel. The client is responsible for cross-process atomicity; the
    JSON backend's file lock has no Redis equivalent here.
    """

    backend_name = "redis"

    def __init__(
        self,
        client: Any | None = None,
        *,
        prefix: str = "gcc:pref:",
        clock: Callable[[], datetime] | None = None,
        ttl_s: float = DEFAULT_PREFERENCE_TTL_S,
        alpha: float = DEFAULT_ALPHA,
        anomaly_sigma: float = ANOMALY_SIGMA,
    ) -> None:
        super().__init__(
            clock=clock, ttl_s=ttl_s, alpha=alpha, anomaly_sigma=anomaly_sigma
        )
        self.client = client
        self.prefix = prefix

    def _key(self, designer_id: str) -> str:
        return f"{self.prefix}{designer_id}"

    def location(self, designer_id: str) -> str:
        return self._key(designer_id)

    def _require_client(self) -> Any:
        if self.client is None:
            raise PreferenceBackendUnavailable(
                "Redis 偏好后端未配置：请注入 redis-py 兼容 client，"
                "或改用 JsonPreferenceStore"
            )
        return self.client

    def _read_payload(self, designer_id: str) -> Mapping[str, Any] | None:
        raw = self._require_client().get(self._key(designer_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PreferenceStoreError(f"Redis 偏好数据损坏：{exc}") from exc
        if not isinstance(payload, Mapping):
            raise PreferenceStoreError("Redis 偏好数据顶层必须是 JSON 对象")
        return payload

    def _write_payload(self, designer_id: str, payload: Mapping[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self._require_client().set(self._key(designer_id), text)

    def _delete_payload(self, designer_id: str) -> bool:
        return bool(self._require_client().delete(self._key(designer_id)))


def open_preference_store(
    backend: str = "json",
    **kwargs: Any,
) -> PreferenceStore:
    """Factory for the pluggable backend switch (``json`` default, ``redis``)."""

    normalized = backend.strip().lower()
    if normalized == "json":
        return JsonPreferenceStore(**kwargs)
    if normalized == "redis":
        return RedisPreferenceStore(**kwargs)
    raise PreferenceValidationError(
        f"未知偏好后端：{backend!r}（可选 json / redis）"
    )


def extract(
    event: CommitEvent | Mapping[str, Any],
    store: PreferenceStore,
) -> ExtractionResult:
    """S6-03 entry point: learn from one commit event and persist the update."""

    return store.extract(event)
