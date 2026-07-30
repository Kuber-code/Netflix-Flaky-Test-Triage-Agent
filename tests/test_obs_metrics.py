"""Metrics persistence, aggregation, and Prometheus rendering."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from flaketriage.models import CauseCode, Classification, DowngradeReason, RunMetadata
from flaketriage.obs.metrics import (
    METRIC_PREFIX,
    CallMetric,
    as_dict,
    render_prometheus,
)
from flaketriage.store.db import IN_MEMORY
from flaketriage.store.repositories import RunStore


@pytest.fixture
def store() -> Iterator[RunStore]:
    with RunStore.open(IN_MEMORY) as opened:
        yield opened


def call(**overrides: object) -> CallMetric:
    defaults: dict[str, object] = {
        "kind": "classify",
        "model": "m",
        "prompt_version": "v1",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.006,
        "latency_ms": 1200.0,
    }
    defaults.update(overrides)
    return CallMetric(**defaults)  # type: ignore[arg-type]


def classification(
    cause: CauseCode = CauseCode.RACE_CONDITION,
    *,
    abstained: bool = False,
    reason: DowngradeReason = DowngradeReason.NONE,
) -> Classification:
    return Classification(
        cause=cause,
        confidence=0.9,
        reasoning="because",
        evidence=("frame",),
        suggested_action="lock it",
        abstained=abstained,
        downgrade_reason=reason,
        model="m",
        prompt_version="v1",
    )


def seed_identity(store: RunStore, name: str = "test_x") -> int:
    from flaketriage.models import TestIdentity

    identity_id, _ = store.upsert_identity(
        TestIdentity(fingerprint=f"fp-{name}", suite_path="tests/a.py", test_name=name)
    )
    return identity_id


# --- persistence -----------------------------------------------------------


def test_calls_and_classifications_are_persisted(store: RunStore) -> None:
    identity_id = seed_identity(store)
    calls, saved = store.record_metrics(
        [call(), call(kind="prefilter")], {identity_id: classification()}
    )
    assert (calls, saved) == (2, 1)

    summary = store.metrics_summary()
    assert summary.api_calls == 2
    assert summary.classifications == 1
    assert summary.total_cost_usd == pytest.approx(0.012)


def test_abstentions_are_stored_not_skipped(store: RunStore) -> None:
    """An abstention rate cannot be computed from a table of successes only."""
    identity_id = seed_identity(store)
    store.record_metrics(
        [call()],
        {
            identity_id: Classification(
                cause=CauseCode.UNKNOWN,
                abstained=True,
                downgrade_reason=DowngradeReason.NO_EVIDENCE,
            )
        },
    )
    summary = store.metrics_summary()
    assert summary.classifications == 1
    assert summary.abstentions == 1
    assert summary.abstention_rate == 1.0
    assert summary.by_downgrade_reason == {"no_evidence": 1}


def test_a_successful_classification_records_no_downgrade_reason(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call()], {identity_id: classification()})
    assert store.metrics_summary().by_downgrade_reason == {}


def test_causes_are_tallied(store: RunStore) -> None:
    first = seed_identity(store, "a")
    second = seed_identity(store, "b")
    store.record_metrics(
        [call(), call()],
        {
            first: classification(CauseCode.RACE_CONDITION),
            second: classification(CauseCode.EXTERNAL_DEPENDENCY),
        },
    )
    assert store.metrics_summary().by_cause == {
        "EXTERNAL_DEPENDENCY": 1,
        "RACE_CONDITION": 1,
    }


def test_failed_calls_are_counted_separately(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call(), call(error="rate_limited")], {identity_id: classification()})
    summary = store.metrics_summary()
    assert summary.api_calls == 2
    assert summary.errors == 1


def test_cache_hits_are_attributed_to_the_right_test(store: RunStore) -> None:
    hit = seed_identity(store, "cached")
    miss = seed_identity(store, "fresh")
    store.record_metrics(
        [call()],
        {hit: classification(), miss: classification()},
        cache_hits=frozenset({hit}),
    )
    summary = store.metrics_summary()
    assert summary.cache_hits == 1
    assert summary.cache_hit_rate == 0.5


def test_the_cache_hit_rate_denominator_is_attempts_not_calls(store: RunStore) -> None:
    """Dividing by API calls would make the rate *rise* as the cache got worse."""
    hit = seed_identity(store, "a")
    miss = seed_identity(store, "b")
    # One call was needed (the miss); the hit needed none.
    store.record_metrics(
        [call()], {hit: classification(), miss: classification()}, cache_hits=frozenset({hit})
    )
    summary = store.metrics_summary()
    assert summary.api_calls == 1
    assert summary.cache_hit_rate == 0.5


def test_latency_percentiles_use_classify_calls_only(store: RunStore) -> None:
    """A prefilter call is fast and would drag the classifier's P50 down."""
    identity_id = seed_identity(store)
    store.record_metrics(
        [
            call(latency_ms=100.0, kind="prefilter"),
            call(latency_ms=1000.0),
            call(latency_ms=5000.0),
        ],
        {identity_id: classification()},
    )
    summary = store.metrics_summary()
    assert summary.latency_p50_ms == 1000.0
    assert summary.latency_p95_ms == 5000.0


def test_failed_calls_are_excluded_from_latency(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics(
        [call(latency_ms=50.0, error="timeout"), call(latency_ms=2000.0)],
        {identity_id: classification()},
    )
    assert store.metrics_summary().latency_p50_ms == 2000.0


def test_the_since_window_excludes_older_activity(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call()], {identity_id: classification()})

    future = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    summary = store.metrics_summary(since=future)
    assert summary.api_calls == 0
    assert summary.classifications == 0
    assert summary.abstention_rate == 0.0


def test_runs_are_counted_in_the_window(store: RunStore) -> None:
    store.record_run(
        RunMetadata(commit_sha="abc", run_id="r1", started_at=datetime(2026, 7, 20, tzinfo=UTC))
    )
    assert store.metrics_summary().runs == 1


def test_an_empty_store_summarizes_to_zeros_without_dividing_by_zero(store: RunStore) -> None:
    summary = store.metrics_summary()
    assert summary.api_calls == 0
    assert summary.abstention_rate == 0.0
    assert summary.cache_hit_rate == 0.0
    assert summary.cost_per_classification_usd == 0.0
    assert summary.latency_p50_ms == 0.0


def test_the_latest_classification_is_retrievable_for_the_policy_engine(
    store: RunStore,
) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call()], {identity_id: classification(CauseCode.RACE_CONDITION)})
    store.record_metrics([call()], {identity_id: classification(CauseCode.TIMING_DEPENDENCY)})

    latest = store.latest_classification(identity_id)
    assert latest is not None
    assert latest.cause is CauseCode.TIMING_DEPENDENCY
    assert latest.evidence == ("frame",)
    assert latest.model == "m"
    assert store.latest_classification(9999) is None


# --- rendering -------------------------------------------------------------


def test_prometheus_output_is_well_formed(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call()], {identity_id: classification()})
    text = render_prometheus(store.metrics_summary())

    for line in text.splitlines():
        assert line.startswith("#") or line.startswith(METRIC_PREFIX), line
    # Each metric declares HELP and TYPE exactly once, which a scraper requires.
    assert text.count(f"# TYPE {METRIC_PREFIX}_llm_latency_ms ") == 1
    assert f"{METRIC_PREFIX}_llm_calls_total 1" in text


def test_prometheus_labels_causes_rather_than_minting_metric_names(store: RunStore) -> None:
    """A taxonomy change must not change the metric namespace."""
    identity_id = seed_identity(store)
    store.record_metrics([call()], {identity_id: classification(CauseCode.RACE_CONDITION)})
    text = render_prometheus(store.metrics_summary())
    assert f'{METRIC_PREFIX}_classifications_by_cause_total{{cause="RACE_CONDITION"}} 1' in text
    assert "race_condition_total" not in text


def test_prometheus_quantiles_are_labels_on_one_metric(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call(latency_ms=1500.0)], {identity_id: classification()})
    text = render_prometheus(store.metrics_summary())
    assert f'{METRIC_PREFIX}_llm_latency_ms{{quantile="0.5"}} 1500' in text
    assert f'{METRIC_PREFIX}_llm_latency_ms{{quantile="0.95"}} 1500' in text


def test_json_dict_is_serializable_and_rounded(store: RunStore) -> None:
    identity_id = seed_identity(store)
    store.record_metrics([call()], {identity_id: classification()})
    payload = as_dict(store.metrics_summary())

    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["api_calls"] == 1
    assert round_tripped["by_cause"] == {"RACE_CONDITION": 1}
    assert round_tripped["abstention_rate"] == 0.0
