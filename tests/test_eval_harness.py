"""Tests for the evaluation harness.

The harness is the artifact the project's credibility rests on, so its metric
arithmetic is tested rather than trusted. A results table computed by an untested
scorer is not evidence of anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from flaketriage.models import CauseCode

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(EVAL_DIR))

import baseline  # noqa: E402
import generate_corpus  # noqa: E402
import metrics  # noqa: E402

CORPUS = json.loads((EVAL_DIR / "dataset" / "corpus.json").read_text(encoding="utf-8"))


# --- corpus ----------------------------------------------------------------


def test_the_corpus_meets_the_specified_minimum() -> None:
    assert len(CORPUS["examples"]) >= 40


def test_every_taxonomy_class_is_represented_including_unknown() -> None:
    labels = {item["label"] for item in CORPUS["examples"]}
    assert labels == {code.value for code in CauseCode}


def test_the_corpus_contains_deliberately_adversarial_cases() -> None:
    """A corpus whose labels are written on their face measures only reading."""
    adversarial = [item for item in CORPUS["examples"] if item["adversarial"]]
    assert len(adversarial) >= 10
    # The specific cases §6.5 asks for.
    assert any(item["label"] == "REAL_REGRESSION" for item in adversarial)
    assert any(item["label"] == "INFRA_FLAKE" for item in adversarial)
    assert any(item["label"] == "UNKNOWN" for item in adversarial)


def test_every_example_carries_a_label_and_a_rationale() -> None:
    for item in CORPUS["examples"]:
        assert item["label"] in {code.value for code in CauseCode}
        assert item["rationale"].strip(), item["id"]


def test_the_corpus_discloses_that_it_is_synthetic() -> None:
    """The disclosure travels with the data, not only with the prose around it."""
    assert CORPUS["synthetic"] is True
    assert "synthetic" in CORPUS["disclosure"].lower()


def test_regeneration_is_deterministic() -> None:
    """The committed corpus must match what the generator produces."""
    assert generate_corpus.build_corpus()["examples"] == CORPUS["examples"]


def test_ids_are_unique() -> None:
    identifiers = [item["id"] for item in CORPUS["examples"]]
    assert len(set(identifiers)) == len(identifiers)


# --- baseline --------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[Errno 104] Connection reset by peer", CauseCode.EXTERNAL_DEPENDENCY),
        ("java.net.UnknownHostException: catalogue.internal", CauseCode.EXTERNAL_DEPENDENCY),
        ("Timeout - Async callback was not invoked", CauseCode.TIMING_DEPENDENCY),
        ("java.lang.OutOfMemoryError: Java heap space", CauseCode.RESOURCE_EXHAUSTION),
        ("duplicate key value violates unique constraint", CauseCode.SHARED_STATE_LEAK),
        ("WARNING: DATA RACE", CauseCode.RACE_CONDITION),
        ("No space left on device", CauseCode.INFRA_FLAKE),
    ],
)
def test_baseline_matches_unambiguous_signatures(message: str, expected: CauseCode) -> None:
    assert (
        baseline.classify(failure_type=None, failure_message=message, stack_trace=None) is expected
    )


def test_baseline_abstains_when_nothing_matches() -> None:
    assert (
        baseline.classify(failure_type=None, failure_message="assert False", stack_trace=None)
        is CauseCode.UNKNOWN
    )
    assert (
        baseline.classify(failure_type=None, failure_message=None, stack_trace=None)
        is CauseCode.UNKNOWN
    )


def test_baseline_rule_order_puts_infra_before_resource() -> None:
    """ "No space left on device" is the platform's fault, not the test's."""
    assert (
        baseline.classify(
            failure_type="OSError", failure_message="No space left on device", stack_trace=None
        )
        is CauseCode.INFRA_FLAKE
    )


def test_baseline_uses_a_single_failing_shard_as_a_last_resort() -> None:
    assert (
        baseline.classify(
            failure_type=None,
            failure_message="assert 4 == 0",
            stack_trace=None,
            failing_shards=("3",),
        )
        is CauseCode.TEST_ORDER_DEPENDENCY
    )


def test_baseline_cannot_recognize_a_regression() -> None:
    """The structural reason its dangerous-error rate is non-zero.

    No keyword distinguishes "this failure is a real defect" from "this failure is
    noise" -- that judgement needs the history and the diff, which a regex cannot
    see. This is the gap the LLM layer exists to fill.
    """
    predicted = baseline.classify(
        failure_type="AssertionError",
        failure_message="assert Decimal('10.00') == Decimal('10.80')",
        stack_trace="src/checkout/totals.py:19: in compute_total",
    )
    assert predicted is not CauseCode.REAL_REGRESSION


# --- metrics ---------------------------------------------------------------


def test_perfect_predictions_score_perfectly() -> None:
    labels = [CauseCode.RACE_CONDITION, CauseCode.INFRA_FLAKE]
    report = metrics.score("t", labels, labels)
    assert report.accuracy == 1.0
    assert report.macro_f1 == 1.0
    assert report.dangerous_error_rate == 0.0


def test_precision_and_recall_are_computed_per_class() -> None:
    labels = [CauseCode.RACE_CONDITION] * 3 + [CauseCode.INFRA_FLAKE]
    predictions = [
        CauseCode.RACE_CONDITION,
        CauseCode.RACE_CONDITION,
        CauseCode.INFRA_FLAKE,
        CauseCode.INFRA_FLAKE,
    ]
    report = metrics.score("t", labels, predictions)

    race = report.per_class[CauseCode.RACE_CONDITION]
    assert race.support == 3
    assert race.recall == pytest.approx(2 / 3)
    assert race.precision == 1.0

    infra = report.per_class[CauseCode.INFRA_FLAKE]
    assert infra.precision == 0.5
    assert infra.recall == 1.0


def test_the_dangerous_error_rate_counts_regressions_called_flakes() -> None:
    """The metric with an asymmetric cost, so it is computed explicitly."""
    labels = [CauseCode.REAL_REGRESSION] * 4
    predictions = [
        CauseCode.RACE_CONDITION,  # dangerous
        CauseCode.TIMING_DEPENDENCY,  # dangerous
        CauseCode.UNKNOWN,  # an abstention is not dangerous
        CauseCode.REAL_REGRESSION,  # correct
    ]
    report = metrics.score("t", labels, predictions)
    assert report.dangerous_errors == 2
    assert report.dangerous_error_rate == 0.5


def test_an_abstention_is_not_a_dangerous_error() -> None:
    """Declining to answer never tells an engineer to ignore a bug."""
    report = metrics.score("t", [CauseCode.REAL_REGRESSION], [CauseCode.UNKNOWN])
    assert report.dangerous_error_rate == 0.0
    assert report.abstention_rate == 1.0


def test_infra_flake_is_not_a_flake_category_for_the_dangerous_metric() -> None:
    report = metrics.score("t", [CauseCode.REAL_REGRESSION], [CauseCode.INFRA_FLAKE])
    assert report.dangerous_errors == 0


def test_abstention_rate_and_conditional_accuracy_are_separate() -> None:
    """Either alone can be improved by moving the confidence floor."""
    labels = [CauseCode.RACE_CONDITION] * 4
    predictions = [
        CauseCode.RACE_CONDITION,
        CauseCode.RACE_CONDITION,
        CauseCode.UNKNOWN,
        CauseCode.TIMING_DEPENDENCY,
    ]
    report = metrics.score("t", labels, predictions)
    assert report.accuracy == 0.5
    assert report.abstention_rate == 0.25
    assert report.accuracy_when_answering == pytest.approx(2 / 3)


def test_adversarial_accuracy_is_tracked_separately() -> None:
    labels = [CauseCode.RACE_CONDITION, CauseCode.INFRA_FLAKE]
    predictions = [CauseCode.RACE_CONDITION, CauseCode.RACE_CONDITION]
    report = metrics.score("t", labels, predictions, adversarial=[False, True])
    assert report.accuracy == 0.5
    assert report.adversarial_total == 1
    assert report.adversarial_accuracy == 0.0


def test_confusion_matrix_records_every_pair() -> None:
    report = metrics.score(
        "t",
        [CauseCode.RACE_CONDITION, CauseCode.RACE_CONDITION],
        [CauseCode.RACE_CONDITION, CauseCode.INFRA_FLAKE],
    )
    assert report.confusion[(CauseCode.RACE_CONDITION, CauseCode.RACE_CONDITION)] == 1
    assert report.confusion[(CauseCode.RACE_CONDITION, CauseCode.INFRA_FLAKE)] == 1


def test_downgrade_reasons_are_tallied_but_none_is_ignored() -> None:
    report = metrics.score(
        "t",
        [CauseCode.RACE_CONDITION] * 3,
        [CauseCode.UNKNOWN, CauseCode.UNKNOWN, CauseCode.RACE_CONDITION],
        downgrade_reasons=["no_evidence", "no_evidence", "none"],
    )
    assert report.downgrade_reasons == {"no_evidence": 2}


def test_macro_f1_ignores_classes_with_no_support() -> None:
    report = metrics.score("t", [CauseCode.RACE_CONDITION], [CauseCode.RACE_CONDITION])
    assert report.macro_f1 == 1.0


def test_empty_input_does_not_divide_by_zero() -> None:
    report = metrics.score("t", [], [])
    assert report.accuracy == 0.0
    assert report.abstention_rate == 0.0
    assert report.dangerous_error_rate == 0.0
    assert report.macro_f1 == 0.0
    assert report.accuracy_when_answering == 0.0


def test_mismatched_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        metrics.score("t", [CauseCode.UNKNOWN], [])


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([], 0.5, 0.0),
        ([5.0], 0.95, 5.0),
        ([1.0, 2.0, 3.0], 0.5, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 0.0, 1.0),
        ([1.0, 2.0, 3.0, 4.0], 1.0, 4.0),
    ],
)
def test_percentile(values: list[float], fraction: float, expected: float) -> None:
    assert metrics.percentile(values, fraction) == expected


# --- committed results -----------------------------------------------------


def test_the_committed_results_table_exists_and_states_its_limits() -> None:
    """The results file is the project's central artifact; it must not overclaim."""
    text = (EVAL_DIR / "results" / "latest.md").read_text(encoding="utf-8")
    assert "synthetic" in text.lower()
    assert "not" in text.lower() and "production-validated" in text.lower()
    for required in (
        "dangerous-error rate",
        "Per-class precision and recall",
        "Confusion matrix",
        "abstention rate",
        "Cost and latency",
        "Where the baseline wins",
    ):
        assert required in text, required
