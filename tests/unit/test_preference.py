"""Preference memory tests (S6-02 / S6-03 / S6-05).

Covers the gate criteria from ``docs/03`` S6 and ``docs/04`` S6-05: sliding
average with alpha=0.3, anomalous samples never update, explicit instructions
win over preferences, TTL, concurrent updates and ``clear``. Every test uses
``tmp_path``; nothing touches the real ``data/memory``.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gameconfig_copilot.memory.preference import (
    DEFAULT_ALPHA,
    CommitEvent,
    JsonPreferenceStore,
    Preference,
    PreferenceBackendUnavailable,
    PreferenceValidationError,
    RedisPreferenceStore,
    extract,
    open_preference_store,
)

DESIGNER = "u_1024"


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment += timedelta(**kwargs)


def _event(
    gold: int | None = 120,
    *,
    commit_id: str = "c1",
    task_type: str = "side",
    signals: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    rewards = [] if gold is None else [{"type": "gold", "amount": gold}]
    return {
        "designer_id": DESIGNER,
        "commit_id": commit_id,
        "task_id": "T_SIDE_0213",
        "draft": {"task_type": task_type, "rewards": rewards},
        "signals": signals or {},
        **extra,
    }


def _store(tmp_path: Any, clock: FakeClock | None = None, **kwargs: Any) -> JsonPreferenceStore:
    return JsonPreferenceStore(
        tmp_path / "prefs",
        clock=clock or FakeClock(),
        **kwargs,
    )


def _reward(preference: Preference) -> dict[str, Any]:
    return preference.fields["default_reward_band"]


# ---------------------------------------------------------------------------
# S6-03 sliding average / sources
# ---------------------------------------------------------------------------


def test_first_commit_bootstraps_mean_and_band(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)

    result = store.extract(_event(120, commit_id="c1"))

    preference = store.get(DESIGNER)
    field = _reward(preference)
    assert field["mean"] == pytest.approx(120.0)
    assert field["sigma"] == pytest.approx(30.0)  # prior: 25% of the observation
    assert field["count"] == 1
    assert field["band"] == {"min": 60, "max": 180}
    assert preference.defaults()["default_reward_band"] == {"min": 60, "max": 180}
    reward_updates = [
        source for source in result.updated if source["field"] == "default_reward_band"
    ]
    assert len(reward_updates) == 1
    assert result.preference.to_dict()["fields"] == preference.to_dict()["fields"]


def test_sliding_average_alpha_0_3_and_versioned_sources(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    assert DEFAULT_ALPHA == 0.3

    store.extract(_event(120, commit_id="c1"))
    clock.advance(minutes=1)
    second = store.extract(_event(140, commit_id="c2"))
    clock.advance(minutes=1)
    store.extract(_event(130, commit_id="c3"))

    field = _reward(store.get(DESIGNER))
    # mean: 0.7*120 + 0.3*140 = 126; then 0.7*126 + 0.3*130 = 127.2
    assert field["mean"] == pytest.approx(127.2)
    # variance: 0.7*900 + 0.3*400 = 750 -> then 0.7*750 + 0.3*16 = 529.8
    assert field["sigma"] == pytest.approx(math.sqrt(529.8), rel=1e-6)
    assert field["count"] == 3
    assert field["band"] == {"min": 81, "max": 174}
    assert field["updated_at"] == clock.moment.isoformat()
    assert field["source_commit"] == "c3"

    sources = [
        source
        for source in store.get(DESIGNER).sources
        if source["field"] == "default_reward_band"
    ]
    assert [source["source_commit"] for source in sources] == ["c1", "c2", "c3"]
    assert set(sources[1]) == {
        "field",
        "old",
        "new",
        "source_commit",
        "updated_at",
    }
    assert sources[1]["field"] == "default_reward_band"
    assert sources[1]["old"] == {"min": 60, "max": 180}
    assert second.updated[0]["source_commit"] == "c2"


def test_band_narrows_as_observations_stay_close(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    widths: list[int] = []
    for index, gold in enumerate((120, 124, 121, 123)):
        store.extract(_event(gold, commit_id=f"c{index}"))
        band = _reward(store.get(DESIGNER))["band"]
        widths.append(band["max"] - band["min"])
    assert widths == sorted(widths, reverse=True)
    assert widths[-1] < widths[0]
    reward_sources = [
        source
        for source in store.get(DESIGNER).sources
        if source["field"] == "default_reward_band"
    ]
    assert len(reward_sources) == 4


# ---------------------------------------------------------------------------
# S6-03 anomaly samples are queued, never applied
# ---------------------------------------------------------------------------


def test_anomaly_does_not_update_and_enters_pending_queue(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.extract(_event(120, commit_id="c1"))
    before = _reward(store.get(DESIGNER))

    clock.advance(minutes=1)
    result = store.extract(_event(1000, commit_id="c2"))

    after = _reward(store.get(DESIGNER))
    assert not any(
        source["field"] == "default_reward_band" for source in result.updated
    )
    assert len(result.anomalies) == 1
    assert after["count"] == before["count"] == 1
    assert after["mean"] == pytest.approx(before["mean"])
    reward_sources = [
        source
        for source in store.get(DESIGNER).sources
        if source["field"] == "default_reward_band"
    ]
    assert len(reward_sources) == 1

    pending = store.pending(DESIGNER)
    assert len(pending) == 1
    sample = pending[0]
    assert sample["field"] == "default_reward_band"
    assert sample["value"] == 1000
    assert sample["source_commit"] == "c2"
    assert sample["threshold"] == pytest.approx(60.0)

    store.resolve_pending(DESIGNER, sample["id"], accept=False)
    assert store.pending(DESIGNER) == []
    assert _reward(store.get(DESIGNER))["mean"] == pytest.approx(120.0)


def test_confirmed_anomaly_is_applied_on_request(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.extract(_event(120, commit_id="c1"))
    store.extract(_event(1000, commit_id="c2"))
    sample = store.pending(DESIGNER)[0]

    clock.advance(minutes=1)
    store.resolve_pending(DESIGNER, sample["id"], accept=True)

    preference = store.get(DESIGNER)
    field = _reward(preference)
    assert field["count"] == 2
    assert field["mean"] == pytest.approx(0.7 * 120 + 0.3 * 1000)
    assert field["band"]["max"] >= 1000
    assert preference.sources[-1]["source_commit"] == "c2"
    assert store.pending(DESIGNER) == []


def test_replayed_anomaly_is_not_duplicated_in_queue(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.extract(_event(120, commit_id="c1"))
    store.extract(_event(1000, commit_id="c2"))
    store.extract(_event(1000, commit_id="c2"))
    assert len(store.pending(DESIGNER)) == 1


# ---------------------------------------------------------------------------
# Explicit instructions always win (docs/03 S6 acceptance)
# ---------------------------------------------------------------------------


def test_explicit_instruction_does_not_update_preference(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.extract(_event(120, commit_id="c1", task_type="side"))
    before = store.get(DESIGNER)

    result = store.extract(
        _event(
            500,
            commit_id="c2",
            task_type="main",
            explicit={"rewards[0].amount": 500},
        )
    )

    after = store.get(DESIGNER)
    assert [entry["field"] for entry in result.skipped_explicit] == [
        "default_reward_band"
    ]
    assert result.skipped_explicit[0]["value"] == 500
    assert _reward(after)["mean"] == pytest.approx(_reward(before)["mean"])
    assert _reward(after)["count"] == 1
    reward_sources = [
        source for source in after.sources if source["field"] == "default_reward_band"
    ]
    assert len(reward_sources) == 1
    # Other fields still learn from the same commit.
    assert "main" in after.defaults()["task_types"]


def test_explicit_value_wins_over_learned_default(tmp_path: Any) -> None:
    store = _store(tmp_path)
    store.extract(_event(120, commit_id="c1"))
    preference = store.get(DESIGNER)

    assert preference.value_for("default_reward_band", explicit=500) == 500
    assert preference.value_for("default_reward_band") == {"min": 60, "max": 180}
    assert preference.value_for("naming_style") is None
    with pytest.raises(PreferenceValidationError):
        preference.value_for("not_a_field")


def test_edit_marked_explicit_is_skipped(tmp_path: Any) -> None:
    store = _store(tmp_path)
    store.extract(_event(120, commit_id="c1"))
    edit = {"field": "rewards[0].amount", "old": 120, "new": 500, "explicit": True}
    result = store.extract(_event(500, commit_id="c2", edits=[edit]))
    assert result.skipped_explicit[0]["field"] == "default_reward_band"
    assert _reward(store.get(DESIGNER))["mean"] == pytest.approx(120.0)
    assert result.human_edits == [edit]


# ---------------------------------------------------------------------------
# Categorical / ordinal fields
# ---------------------------------------------------------------------------


def test_task_types_and_naming_style_use_weighted_average(tmp_path: Any) -> None:
    store = _store(tmp_path)
    store.extract(_event(120, commit_id="c1", task_type="side"))
    store.extract(_event(120, commit_id="c2", task_type="side"))
    store.extract(_event(120, commit_id="c3", task_type="main"))

    preference = store.get(DESIGNER)
    assert preference.defaults()["task_types"] == ["side", "main"]
    weights = preference.fields["task_types"]["weights"]
    assert weights["side"] == pytest.approx(0.7)
    assert weights["main"] == pytest.approx(0.3)

    store.extract(
        _event(120, commit_id="c4", signals={"naming_style": "snake_case"})
    )
    store.extract(
        _event(120, commit_id="c5", signals={"naming_style": "snake_case"})
    )
    store.extract(
        _event(120, commit_id="c6", signals={"naming_style": "camelCase"})
    )
    preference = store.get(DESIGNER)
    assert preference.defaults()["naming_style"] == "snake_case"
    assert set(preference.fields["naming_style"]["top"]) == {"snake_case", "camelCase"}


def test_strictness_is_ordinal_and_maps_to_label(tmp_path: Any) -> None:
    store = _store(tmp_path)
    store.extract(_event(120, commit_id="c1", signals={"strictness": "high"}))
    assert store.get(DESIGNER).defaults()["strictness"] == "high"
    assert _reward(store.get(DESIGNER))["count"] == 1

    store.extract(_event(120, commit_id="c2", signals={"strictness": "medium"}))
    field = store.get(DESIGNER).fields["strictness"]
    assert field["mean"] == pytest.approx(0.85)
    assert field["label"] == "high"

    store.extract(_event(120, commit_id="c3", signals={"strictness": "medium"}))
    assert store.get(DESIGNER).fields["strictness"]["mean"] == pytest.approx(
        0.7 * 0.85 + 0.3 * 0.5
    )


# ---------------------------------------------------------------------------
# Storage: show / clear / TTL / validation
# ---------------------------------------------------------------------------


def test_show_and_clear_remove_everything(tmp_path: Any) -> None:
    store = _store(tmp_path)
    store.extract(_event(120, commit_id="c1"))
    store.extract(_event(1000, commit_id="c2"))  # pending queue populated

    view = store.show(DESIGNER)
    assert view["backend"] == "json"
    assert view["defaults"]["default_reward_band"] == {"min": 60, "max": 180}
    reward_sources = [
        source for source in view["sources"] if source["field"] == "default_reward_band"
    ]
    assert len(reward_sources) == 1
    assert len(view["pending"]) == 1
    assert view["expired"] is False

    assert store.clear(DESIGNER) is True
    cleared = store.get(DESIGNER)
    assert cleared.fields == {}
    assert cleared.sources == []
    assert cleared.pending == []
    assert store.show(DESIGNER)["defaults"] == {}
    assert store.clear(DESIGNER) is False


def test_preference_ttl_expires_after_90_days(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock, ttl_s=90 * 24 * 60 * 60)
    store.extract(_event(120, commit_id="c1"))

    clock.advance(days=89)
    assert store.get(DESIGNER).defaults()["default_reward_band"] == {
        "min": 60,
        "max": 180,
    }

    clock.advance(days=2)
    expired = store.get(DESIGNER)
    assert expired.expired is True
    assert expired.fields == {}
    assert store.show(DESIGNER)["expired"] is True

    # A new commit starts a fresh preference instead of reviving stale state.
    store.extract(_event(150, commit_id="c2"))
    refreshed = store.get(DESIGNER)
    assert refreshed.expired is False
    assert _reward(refreshed)["count"] == 1
    assert _reward(refreshed)["mean"] == pytest.approx(150.0)


def test_concurrent_extractions_do_not_lose_updates(tmp_path: Any) -> None:
    clock = FakeClock()
    store = _store(tmp_path, clock)
    values = [120, 121, 122, 123, 124, 125, 126, 127]

    def run(index: int) -> None:
        extract(_event(values[index], commit_id=f"c{index}"), store)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(len(values))))

    preference = store.get(DESIGNER)
    assert _reward(preference)["count"] == len(values)
    reward_sources = [
        source
        for source in preference.sources
        if source["field"] == "default_reward_band"
    ]
    assert len(reward_sources) == len(values)
    assert preference.pending == []
    assert 120 <= _reward(preference)["mean"] <= 127


def test_invalid_events_fail_closed(tmp_path: Any) -> None:
    store = _store(tmp_path)
    with pytest.raises(PreferenceValidationError):
        store.extract({"commit_id": "c1", "draft": {}})
    with pytest.raises(PreferenceValidationError):
        store.extract({"designer_id": DESIGNER, "draft": {}})


def test_extract_function_accepts_commit_event(tmp_path: Any) -> None:
    store = _store(tmp_path)
    event = CommitEvent(
        designer_id=DESIGNER,
        commit_id="c1",
        draft={"task_type": "side", "rewards": [{"type": "gold", "amount": 200}]},
    )
    result = extract(event, store)
    assert result.preference.defaults()["default_reward_band"] == {
        "min": 100,
        "max": 300,
    }


# ---------------------------------------------------------------------------
# Pluggable backend
# ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> bool:
        self.data[key] = value
        return True

    def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


def test_redis_backend_is_pluggable_with_injected_client() -> None:
    store = RedisPreferenceStore(FakeRedis(), clock=FakeClock())
    store.extract(_event(120, commit_id="c1"))
    assert store.get(DESIGNER).defaults()["default_reward_band"] == {
        "min": 60,
        "max": 180,
    }
    assert store.clear(DESIGNER) is True
    assert store.get(DESIGNER).fields == {}


def test_redis_backend_without_client_fails_closed() -> None:
    store = RedisPreferenceStore()
    with pytest.raises(PreferenceBackendUnavailable):
        store.get(DESIGNER)
    with pytest.raises(PreferenceBackendUnavailable):
        store.extract(_event(120, commit_id="c1"))


def test_open_preference_store_selects_backend(tmp_path: Any) -> None:
    json_store = open_preference_store("json", root=tmp_path / "prefs")
    assert isinstance(json_store, JsonPreferenceStore)
    redis_store = open_preference_store("redis")
    assert isinstance(redis_store, RedisPreferenceStore)
    with pytest.raises(PreferenceValidationError):
        open_preference_store("sqlite")
