"""Classifier orchestration: cache, budget, prefilter, repair retry.

Each behaviour here is an answer to one of §12's reviewer questions, and each is
tested against a scripted client because a live API will not produce a malformed
response, a rate limit, or an exhausted budget on request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeClient, error_response, valid_response
from flaketriage.classify.cache import ClassificationCache, context_key
from flaketriage.classify.classifier import Classifier
from flaketriage.classify.pricing import CostTable
from flaketriage.classify.prompt import prompt_version_hash
from flaketriage.classify.schema import DowngradeReason
from flaketriage.classify.taxonomy import CauseCode
from flaketriage.config import ClassifyConfig
from flaketriage.detect.detector import detect_for_history
from flaketriage.detect.models import Detection
from flaketriage.identity.fingerprint import fingerprint
from flaketriage.models import Outcome, TestIdentity
from helpers import MIXED, PASS, history_from

IDENTITY = TestIdentity(
    fingerprint=fingerprint("tests/test_auth.py", "test_login"),
    suite_path="tests/test_auth.py",
    test_name="test_login",
    file_path="tests/test_auth.py",
)

TRACE = (
    "tests/test_auth.py:88: in test_login\n"
    "    assert session.count == 1\n"
    "E   AssertionError: assert 2 == 1\n"
    "src/auth/session.py:44: in bump"
)

# Prefilter off by default so each test exercises one thing; the prefilter has
# its own section below.
CONFIG = ClassifyConfig(prefilter_enabled=False)

PRICES = CostTable({"fake-classifier": (3.0, 15.0), "fake-prefilter": (1.0, 5.0)})


def detection(*, trace: str | None = TRACE) -> Detection:
    history = history_from([("a", PASS), ("b", MIXED)], stack_trace=trace)
    return detect_for_history(IDENTITY, 1, history)


def make(
    responses: list[object],
    *,
    config: ClassifyConfig = CONFIG,
    cache: ClassificationCache | None = None,
    budget_usd: float | None = None,
) -> tuple[Classifier, FakeClient]:
    client = FakeClient(responses)  # type: ignore[arg-type]
    classifier = Classifier(
        client,
        config.model_copy(
            update={"classifier_model": "fake-classifier", "prefilter_model": "fake-prefilter"}
        ),
        cache=cache,
        cost_table=PRICES,
        budget_usd=budget_usd,
    )
    return classifier, client


# --- the ordinary path -----------------------------------------------------


def test_a_valid_response_is_classified() -> None:
    classifier, client = make([valid_response()])
    result = classifier.classify(detection())

    assert result.cause is CauseCode.RACE_CONDITION
    assert result.abstained is False
    assert client.call_count == 1


def test_the_model_and_prompt_version_are_recorded_on_every_result() -> None:
    """Results must be attributable to a model and a prompt, per §6.4."""
    classifier, _ = make([valid_response()])
    result = classifier.classify(detection())
    assert result.model == "fake-classifier"
    assert result.prompt_version == prompt_version_hash()
    assert result.prompt_version.startswith("2026-")


def test_temperature_zero_is_requested() -> None:
    classifier, client = make([valid_response()])
    classifier.classify(detection())
    assert client.calls[0]["temperature"] == 0.0


def test_the_schema_is_sent_with_the_request() -> None:
    classifier, client = make([valid_response()])
    classifier.classify(detection())
    schema = client.calls[0]["schema"]
    assert schema is not None
    assert schema["additionalProperties"] is False


def test_cost_and_latency_are_accounted() -> None:
    classifier, _ = make([valid_response()])
    classifier.classify(detection())

    assert classifier.stats.api_calls == 1
    # 400 in @ $3/Mtok + 80 out @ $15/Mtok
    assert classifier.stats.total_cost_usd == (400 * 3.0 + 80 * 15.0) / 1_000_000
    assert classifier.stats.latencies_ms() == [25.0]


# --- malformed output and the repair retry ---------------------------------


def test_a_malformed_response_is_repaired_once() -> None:
    classifier, client = make(["this is not json", valid_response()])
    result = classifier.classify(detection())

    assert client.call_count == 2
    assert result.cause is CauseCode.RACE_CONDITION
    assert classifier.stats.repairs_attempted == 1
    assert classifier.stats.repairs_succeeded == 1


def test_the_repair_prompt_says_what_was_wrong() -> None:
    classifier, client = make(["not json", valid_response()])
    classifier.classify(detection())

    repair_prompt = client.calls[1]["user"]
    assert "could not be parsed" in repair_prompt
    assert "invalid_json" in repair_prompt
    assert "not json" in repair_prompt  # the offending response is shown back


def test_two_malformed_responses_abstain_rather_than_raise() -> None:
    """Phase P4's exit criterion, stated as a test."""
    classifier, client = make(["garbage", "still garbage"])
    result = classifier.classify(detection())

    assert client.call_count == 2
    assert result.cause is CauseCode.UNKNOWN
    assert result.abstained is True
    assert result.downgrade_reason is DowngradeReason.SCHEMA_INVALID
    assert classifier.stats.repairs_succeeded == 0


def test_a_downgraded_answer_is_not_retried() -> None:
    """A confident answer with no evidence is complete, just untrustworthy.

    Retrying it would spend a second call re-asking a question the model already
    answered in schema-valid form.
    """
    classifier, client = make([valid_response(evidence=[])])
    result = classifier.classify(detection())

    assert client.call_count == 1
    assert result.downgrade_reason is DowngradeReason.NO_EVIDENCE


def test_an_invented_cause_triggers_a_repair() -> None:
    classifier, client = make([valid_response(cause="RACEY_THING"), valid_response()])
    result = classifier.classify(detection())
    assert client.call_count == 2
    assert result.cause is CauseCode.RACE_CONDITION


# --- API failures ----------------------------------------------------------


def test_an_api_error_abstains_and_never_raises() -> None:
    """An unavailable API must degrade the run, not fail the build."""
    classifier, _ = make([error_response("rate_limited")])
    result = classifier.classify(detection())

    assert result.cause is CauseCode.UNKNOWN
    assert result.downgrade_reason is DowngradeReason.API_ERROR
    assert "rate_limited" in result.reasoning


def test_an_api_error_during_repair_abstains() -> None:
    classifier, _ = make(["garbage", error_response("timeout")])
    result = classifier.classify(detection())
    assert result.downgrade_reason is DowngradeReason.API_ERROR


def test_an_empty_response_abstains() -> None:
    classifier, _ = make([None, None])
    result = classifier.classify(detection())
    assert result.cause is CauseCode.UNKNOWN


# --- budget ----------------------------------------------------------------


def test_budget_exhaustion_degrades_gracefully_with_a_reason() -> None:
    classifier, client = make([valid_response()], budget_usd=0.0)
    result = classifier.classify(detection())

    assert client.call_count == 0, "the ceiling must be checked before spending"
    assert result.cause is CauseCode.UNKNOWN
    assert result.downgrade_reason is DowngradeReason.BUDGET_EXHAUSTED
    assert "budget" in result.reasoning


def test_budget_stops_a_batch_partway_and_marks_the_rest() -> None:
    """Remaining items are emitted as UNKNOWN, never silently truncated."""
    # One call costs $0.0021; a $0.003 ceiling allows exactly one.
    classifier, client = make([valid_response()], budget_usd=0.003)
    detections = [detection().model_copy(update={"identity_id": index}) for index in range(4)]
    results = classifier.classify_many(detections)

    assert len(results) == 4, "every requested test must appear in the output"
    assert client.call_count == 1
    exhausted = [
        result
        for result in results.values()
        if result.downgrade_reason is DowngradeReason.BUDGET_EXHAUSTED
    ]
    assert len(exhausted) == 3


def test_the_batch_cap_marks_the_overflow_rather_than_dropping_it() -> None:
    classifier, client = make([valid_response()])
    detections = [detection().model_copy(update={"identity_id": index}) for index in range(5)]
    results = classifier.classify_many(detections, max_tests=2)

    assert len(results) == 5
    assert client.call_count == 2
    assert (
        sum(
            1
            for result in results.values()
            if result.downgrade_reason is DowngradeReason.BUDGET_EXHAUSTED
        )
        == 3
    )


def test_the_batch_is_prioritized_by_flake_rate() -> None:
    """Limited budget goes to the tests most worth explaining."""
    low = detect_for_history(IDENTITY, 1, history_from([("a", PASS), ("b", MIXED)]))
    high = detect_for_history(IDENTITY, 2, history_from([("a", MIXED), ("b", MIXED), ("c", MIXED)]))
    assert high.flake_rate > low.flake_rate

    classifier, _ = make([valid_response()])
    results = classifier.classify_many([low, high], max_tests=1)

    assert results[2].cause is CauseCode.RACE_CONDITION
    assert results[1].downgrade_reason is DowngradeReason.BUDGET_EXHAUSTED


# --- cache -----------------------------------------------------------------


def test_an_identical_context_costs_nothing_the_second_time(tmp_path: Path) -> None:
    cache = ClassificationCache(tmp_path / "cache")
    classifier, client = make([valid_response()], cache=cache)

    first = classifier.classify(detection())
    second = classifier.classify(detection())

    assert client.call_count == 1
    assert first.cause is second.cause
    assert classifier.stats.cache_hits == 1
    assert classifier.stats.cache_hit_rate == 0.5


def test_a_different_failure_is_a_cache_miss(tmp_path: Path) -> None:
    cache = ClassificationCache(tmp_path / "cache")
    classifier, client = make([valid_response()], cache=cache)

    classifier.classify(detection())
    classifier.classify(detection(trace="src/other.py:1: in other\nE   ValueError"))

    assert client.call_count == 2
    assert classifier.stats.cache_hits == 0


def test_the_cache_key_covers_model_and_prompt_version() -> None:
    """A prompt edit must not serve yesterday's answers."""
    base = context_key("ctx", model="m1", prompt_version="v1")
    assert context_key("ctx", model="m2", prompt_version="v1") != base
    assert context_key("ctx", model="m1", prompt_version="v2") != base
    assert context_key("other", model="m1", prompt_version="v1") != base
    assert context_key("ctx", model="m1", prompt_version="v1") == base


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = ClassificationCache(tmp_path / "cache")
    classifier, client = make([valid_response()], cache=cache)
    classifier.classify(detection())

    for entry in (tmp_path / "cache").rglob("*.json"):
        entry.write_text("{ truncated", encoding="utf-8")

    classifier.classify(detection())
    assert client.call_count == 2  # re-classified rather than raising


def test_transient_abstentions_are_not_cached(tmp_path: Path) -> None:
    """One bad afternoon must not become a permanent answer."""
    cache = ClassificationCache(tmp_path / "cache")
    classifier, _ = make([error_response("rate_limited")], cache=cache)
    classifier.classify(detection())

    assert list((tmp_path / "cache").rglob("*.json")) == []


def test_a_genuine_abstention_is_cached(tmp_path: Path) -> None:
    """UNKNOWN from thin evidence is a real answer about that evidence."""
    cache = ClassificationCache(tmp_path / "cache")
    classifier, client = make([valid_response(cause="UNKNOWN", abstained=True)], cache=cache)

    classifier.classify(detection())
    classifier.classify(detection())
    assert client.call_count == 1


def test_a_disabled_cache_never_reports_hits(tmp_path: Path) -> None:
    cache = ClassificationCache(tmp_path / "cache", enabled=False)
    classifier, client = make([valid_response()], cache=cache)
    classifier.classify(detection())
    classifier.classify(detection())
    assert client.call_count == 2
    assert cache.hit_rate == 0.0


# --- prefilter -------------------------------------------------------------

PREFILTER_ON = ClassifyConfig(prefilter_enabled=True)


YES_GATE = '{"classifiable": true}'
NO_GATE = '{"classifiable": false}'


def test_the_prefilter_lets_a_specific_failure_through() -> None:
    classifier, client = make([YES_GATE, valid_response()], config=PREFILTER_ON)
    result = classifier.classify(detection())

    assert client.call_count == 2
    assert client.calls[0]["model"] == "fake-prefilter"
    assert client.calls[1]["model"] == "fake-classifier"
    assert result.cause is CauseCode.RACE_CONDITION


def test_the_prefilter_stops_an_unclassifiable_failure() -> None:
    classifier, client = make([NO_GATE], config=PREFILTER_ON)
    result = classifier.classify(detection(trace=None))

    assert client.call_count == 1, "the expensive model must not be called"
    assert result.cause is CauseCode.UNKNOWN
    assert result.downgrade_reason is DowngradeReason.PREFILTERED
    assert classifier.stats.prefiltered == 1


def test_a_prefilter_failure_escalates_rather_than_dropping_the_test() -> None:
    """A wrong NO is invisible; a wrong YES costs one call. The asymmetry decides."""
    classifier, client = make([error_response("timeout"), valid_response()], config=PREFILTER_ON)
    result = classifier.classify(detection())

    assert client.call_count == 2
    assert result.cause is CauseCode.RACE_CONDITION


def test_an_unparseable_prefilter_reply_escalates() -> None:
    """Every gate ambiguity resolves toward spending the call.

    A misparsed rejection loses a classification silently, which is the failure
    mode that shows up in no metric.
    """
    for reply in ("maybe?", "", "{}", '{"classifiable": "yes"}', "null", "[]"):
        classifier, client = make([reply, valid_response()], config=PREFILTER_ON)
        assert classifier.classify(detection()).cause is CauseCode.RACE_CONDITION, reply
        assert client.call_count == 2, reply


def test_the_prefilter_uses_a_tiny_output_budget_and_a_boolean_schema() -> None:
    """A boolean schema, because the model will not obey "reply YES or NO"."""
    classifier, client = make([YES_GATE, valid_response()], config=PREFILTER_ON)
    classifier.classify(detection())
    assert client.calls[0]["max_tokens"] == PREFILTER_ON.prefilter_max_output_tokens
    assert client.calls[0]["schema"]["properties"] == {
        "classifiable": {
            "type": "boolean",
            "description": "True if the input contains any usable failure detail.",
        }
    }


def test_a_named_exception_always_clears_the_gate_prompt() -> None:
    """The gate asks about evidence presence, not about cause certainty.

    The first version asked the latter, and Haiku rejected a ConnectionResetError
    with a full trace as "a generic network error".
    """
    from flaketriage.classify.prompt import PREFILTER_SYSTEM_PROMPT

    assert "NOT deciding what" in PREFILTER_SYSTEM_PROMPT
    assert "named exception type always counts" in PREFILTER_SYSTEM_PROMPT


# --- disabled --------------------------------------------------------------


def test_a_classifier_with_no_client_abstains_without_calling_anything() -> None:
    classifier = Classifier(None, CONFIG)
    result = classifier.classify(detection())

    assert classifier.enabled is False
    assert result.downgrade_reason is DowngradeReason.LLM_DISABLED
    assert classifier.stats.api_calls == 0


def test_skips_are_not_classified_by_needs_classification() -> None:
    healthy = detect_for_history(IDENTITY, 1, history_from([("a", PASS)]))
    assert healthy.needs_classification is False
    assert detection().needs_classification is True


def test_stats_track_everything_the_metrics_table_needs() -> None:
    classifier, _ = make([valid_response()])
    classifier.classify(detection())
    stats = classifier.stats

    assert stats.api_calls == 1
    assert stats.total_cost_usd > 0
    assert stats.latencies_ms("classify") == [25.0]
    assert stats.schema_failures == 0
    assert stats.prefiltered == 0


def test_unknown_model_prices_cost_zero_rather_than_raising() -> None:
    classifier = Classifier(
        FakeClient([valid_response()]),
        CONFIG.model_copy(update={"classifier_model": "some-unpriced-model"}),
        cost_table=PRICES,
    )
    result = classifier.classify(detection())
    assert result.cause is CauseCode.RACE_CONDITION
    assert classifier.stats.total_cost_usd == 0.0


def test_outcome_kind_is_explained_in_the_prompt() -> None:
    """The failure/error distinction feeds classification, so it must be stated."""
    classifier, client = make([valid_response()])
    classifier.classify(detection())
    prompt = client.calls[0]["user"]
    assert "assertion failure" in prompt
    assert "UNKNOWN" in client.calls[0]["system"]


def test_error_outcomes_are_described_as_harness_errors() -> None:
    history = history_from([("a", PASS), ("b", [Outcome.PASS, Outcome.ERROR])])
    classifier, client = make([valid_response()])
    classifier.classify(detect_for_history(IDENTITY, 1, history))
    assert "harness error" in client.calls[0]["user"]


def test_spend_never_exceeds_the_ceiling_by_more_than_one_call() -> None:
    """The documented guarantee: a call's cost is unknown until it returns."""
    classifier, _ = make([valid_response()], budget_usd=0.005)
    detections = [detection().model_copy(update={"identity_id": index}) for index in range(20)]
    classifier.classify_many(detections, max_tests=20)

    one_call = (400 * 3.0 + 80 * 15.0) / 1_000_000
    assert classifier.stats.total_cost_usd <= 0.005 + one_call


def test_a_generous_budget_classifies_everything() -> None:
    classifier, client = make([valid_response()], budget_usd=10.0)
    detections = [detection().model_copy(update={"identity_id": index}) for index in range(6)]
    results = classifier.classify_many(detections, max_tests=10)

    assert client.call_count == 6
    assert all(result.cause is CauseCode.RACE_CONDITION for result in results.values())


def test_the_cache_key_changes_when_the_gate_model_changes(tmp_path: Path) -> None:
    """The gate can turn a classifiable failure into an abstention.

    Two runs with different gate models are therefore not interchangeable, even
    when the expensive model is identical. Missing this meant a fixed prefilter
    prompt kept serving rejections produced by the broken one.
    """
    cache = ClassificationCache(tmp_path / "cache")

    def build(gate_model: str, responses: list[object]) -> tuple[Classifier, FakeClient]:
        client = FakeClient(responses)  # type: ignore[arg-type]
        config = PREFILTER_ON.model_copy(
            update={"classifier_model": "fake-classifier", "prefilter_model": gate_model}
        )
        return Classifier(client, config, cache=cache, cost_table=PRICES), client

    first, first_client = build("gate-a", [NO_GATE])
    assert first.classify(detection()).downgrade_reason is DowngradeReason.PREFILTERED
    assert first_client.call_count == 1

    second, second_client = build("gate-b", [YES_GATE, valid_response()])
    result = second.classify(detection())

    assert second_client.call_count == 2, "a different gate model must not reuse the entry"
    assert result.cause is CauseCode.RACE_CONDITION


def test_the_prompt_version_covers_the_prefilter_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing the gate's wording must invalidate cached gate decisions."""
    from flaketriage.classify import prompt as prompt_module

    baseline = prompt_module.prompt_version_hash()
    monkeypatch.setattr(
        prompt_module,
        "PREFILTER_SYSTEM_PROMPT",
        prompt_module.PREFILTER_SYSTEM_PROMPT + " extra guidance",
    )
    assert prompt_module.prompt_version_hash() != baseline
