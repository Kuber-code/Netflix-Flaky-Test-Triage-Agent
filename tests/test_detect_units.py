"""Unit tests for the detector's building blocks: infra, footprint, rates."""

from __future__ import annotations

import pytest

from flaketriage.config import DetectConfig
from flaketriage.detect.footprint import (
    diff_touches_footprint,
    extract_paths,
    footprint,
    is_project_frame,
)
from flaketriage.detect.history import WindowStatus, build_history
from flaketriage.detect.infra import is_infra_failure
from flaketriage.detect.rates import divergence_rate, ewma, flake_rate, intermittency_rate
from flaketriage.models import Outcome
from flaketriage.store.repositories import ExecutionRecord
from helpers import record

PYTEST_TRACE = """\
self = <tests.unit.test_login.TestLogin object at 0x7f2c1d0>

    def test_login_retries(self):
>       assert response.status_code == 200
E       AssertionError

tests/unit/test_login.py:27: AssertionError
/opt/hostedtoolcache/Python/3.12.1/x64/lib/python3.12/site-packages/requests/api.py:59: in post
    return request("post", url, data=data, json=json, **kwargs)
"""

JAVA_TRACE = """\
java.lang.AssertionError: expected:<1> but was:<2>
\tat com.example.orders.OrderServiceTest.chargesCardOnce(OrderServiceTest.java:88)
\tat com.example.orders.PaymentsClient.charge(PaymentsClient.java:47)
\tat java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:642)
"""


# --- infra exclusion -------------------------------------------------------


def test_platform_errors_are_recognized() -> None:
    assert is_infra_failure(
        record(outcome=Outcome.ERROR, failure_message="No space left on device")
    )
    assert is_infra_failure(
        record(outcome=Outcome.ERROR, stack_trace="the runner has received a shutdown signal")
    )


def test_an_assertion_failure_is_never_infrastructure() -> None:
    """If the framework evaluated an assertion, the harness was working."""
    assert (
        is_infra_failure(record(outcome=Outcome.FAIL, failure_message="no space left on device"))
        is False
    )


def test_an_ordinary_error_is_not_infrastructure() -> None:
    assert (
        is_infra_failure(record(outcome=Outcome.ERROR, failure_type="ConnectionResetError"))
        is False
    )


def test_a_pass_is_not_infrastructure() -> None:
    assert is_infra_failure(record(outcome=Outcome.PASS)) is False


def test_infra_patterns_come_from_config() -> None:
    custom = DetectConfig(infra_error_patterns=("gremlins in the datacentre",))
    candidate = record(outcome=Outcome.ERROR, failure_message="Gremlins in the datacentre again")
    assert is_infra_failure(candidate, custom) is True
    assert is_infra_failure(candidate, DetectConfig()) is False


def test_infra_failures_are_excluded_from_the_flake_rate_entirely() -> None:
    """Numerator and denominator both: the observation never happened."""
    records = [
        record(outcome=Outcome.PASS, sha="a", execution_id=1),
        record(
            outcome=Outcome.ERROR,
            sha="a",
            attempt=2,
            failure_message="ImagePullBackOff",
            execution_id=2,
        ),
    ]
    history = build_history(records)

    assert history.infra_excluded == 1
    assert history.observations == 1
    # Without the exclusion this commit would look divergent and the test would
    # be blamed for an image-pull failure.
    assert history.windows[0].diverged is False
    assert flake_rate(history) == 0.0


# --- footprint -------------------------------------------------------------


def test_project_frames_are_extracted_and_framework_noise_is_dropped() -> None:
    paths = extract_paths(PYTEST_TRACE)
    assert "tests/unit/test_login.py" in paths
    assert not any("site-packages" in path for path in paths)


def test_java_frames_yield_filenames() -> None:
    paths = extract_paths(JAVA_TRACE)
    assert "OrderServiceTest.java" in paths
    assert "PaymentsClient.java" in paths
    assert "ThreadPoolExecutor.java" not in paths  # java.base noise


def test_extraction_preserves_first_appearance_order() -> None:
    paths = extract_paths(JAVA_TRACE)
    assert paths.index("OrderServiceTest.java") < paths.index("PaymentsClient.java")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/unit/test_a.py", True),
        ("src/orders/service.py", True),
        (".venv/lib/site-packages/requests/api.py", False),
        ("node_modules/jest/build/index.js", False),
        ("/usr/lib/python3.12/unittest/case.py", False),
        ("", False),
    ],
)
def test_is_project_frame(path: str, expected: bool) -> None:
    assert is_project_frame(path) is expected


def test_footprint_always_includes_the_test_file() -> None:
    """A change to the test itself is the most direct explanation there is."""
    result = footprint(test_file="tests/unit/test_login.py", stack_trace=None)
    assert result == {"tests/unit/test_login.py"}


def test_footprint_combines_test_file_and_trace() -> None:
    result = footprint(test_file="tests/unit/test_login.py", stack_trace=JAVA_TRACE)
    assert "tests/unit/test_login.py" in result
    assert "PaymentsClient.java" in result


def test_footprint_matching_tolerates_path_shape() -> None:
    known = frozenset({"PaymentsClient.java"})
    assert diff_touches_footprint(
        frozenset({"src/main/java/com/example/PaymentsClient.java"}), known
    )
    assert diff_touches_footprint(frozenset({"src/other/Thing.java"}), known) is False


def test_footprint_matching_handles_empty_sides() -> None:
    assert diff_touches_footprint(frozenset(), frozenset({"a.py"})) is False
    assert diff_touches_footprint(frozenset({"a.py"}), frozenset()) is False


# --- rates -----------------------------------------------------------------


def test_ewma_of_an_empty_series_is_zero() -> None:
    assert ewma([], 0.3) == 0.0


def test_ewma_of_a_constant_series_is_that_constant() -> None:
    assert ewma([1.0] * 10, 0.3) == pytest.approx(1.0)
    assert ewma([0.0] * 10, 0.3) == pytest.approx(0.0)


def test_ewma_decays_a_bad_history_geometrically() -> None:
    """A test fixed last week must not stay condemned by a bad month.

    Each clean observation multiplies the estimate by ``1 - alpha``, so a week of
    green runs clears a month of red. A plain window mean would still be near 0.6
    after the same five clean runs, which is the behaviour being avoided.
    """
    bad_month = [1.0] * 20
    after_one = ewma([*bad_month, 0.0], 0.3)
    after_five = ewma([*bad_month, *([0.0] * 5)], 0.3)
    after_ten = ewma([*bad_month, *([0.0] * 10)], 0.3)

    assert after_one == pytest.approx(0.7)
    assert after_five == pytest.approx(0.7**5, abs=1e-9)
    assert after_ten < 0.03
    assert after_ten < after_five < after_one

    window_mean = sum([*bad_month, *([0.0] * 5)]) / 25
    assert window_mean > after_five


def test_ewma_reacts_to_a_newly_unstable_test() -> None:
    assert ewma([0.0] * 20 + [1.0], 0.3) == pytest.approx(0.3)
    assert ewma([0.0] * 20 + [1.0, 1.0, 1.0], 0.3) > 0.65


def test_divergence_rate_is_zero_without_any_divergence() -> None:
    single_observations = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", started_at="2026-07-20T10:00:00+00:00"),
            record(outcome=Outcome.FAIL, sha="b", started_at="2026-07-20T11:00:00+00:00"),
        ]
    )
    assert single_observations.retry_data_available is False
    assert divergence_rate(single_observations) == 0.0


def test_divergence_rate_divides_by_all_commits_not_just_retried_ones() -> None:
    """Real pipelines retry only on failure.

    Narrowing the denominator to retried commits would make every retried commit
    a failed commit, and the rate would read near 100% for a test that fails one
    run in ten. This is the specific mistake being guarded against.
    """
    records: list[ExecutionRecord] = []
    for index in range(9):
        records.append(
            record(
                outcome=Outcome.PASS,
                sha=f"clean{index}",
                started_at=f"2026-07-20T{index:02d}:00:00+00:00",
                execution_id=len(records) + 1,
            )
        )
    # One commit failed, was retried, and passed on the retry.
    records.append(
        record(
            outcome=Outcome.FAIL,
            sha="retried",
            started_at="2026-07-20T09:00:00+00:00",
            execution_id=len(records) + 1,
        )
    )
    records.append(
        record(
            outcome=Outcome.PASS,
            sha="retried",
            attempt=2,
            started_at="2026-07-20T09:00:00+00:00",
            execution_id=len(records) + 1,
        )
    )
    history = build_history(records)

    assert len(history.measurable_windows) == 1
    assert history.retry_data_available is True
    # One divergence in ten commits, weighted toward the recent one -- not 100%.
    assert divergence_rate(history) == pytest.approx(0.3)


def test_divergence_rate_measures_same_commit_disagreement() -> None:
    history = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", execution_id=1),
            record(outcome=Outcome.FAIL, sha="a", attempt=2, execution_id=2),
        ]
    )
    assert history.retry_data_available is True
    assert divergence_rate(history) == pytest.approx(1.0)


def test_intermittency_rate_counts_flips_between_commits() -> None:
    records = []
    for index, outcome in enumerate([Outcome.PASS, Outcome.FAIL, Outcome.PASS, Outcome.FAIL]):
        records.append(
            record(
                outcome=outcome,
                sha=f"sha{index}",
                started_at=f"2026-07-20T1{index}:00:00+00:00",
                execution_id=index + 1,
            )
        )
    assert intermittency_rate(build_history(records)) == pytest.approx(1.0)


def test_intermittency_rate_is_zero_for_a_stable_test() -> None:
    records = [
        record(
            outcome=Outcome.PASS,
            sha=f"sha{index}",
            started_at=f"2026-07-20T1{index}:00:00+00:00",
            execution_id=index + 1,
        )
        for index in range(4)
    ]
    assert intermittency_rate(build_history(records)) == 0.0


def test_flake_rate_prefers_divergence_and_falls_back_to_flips() -> None:
    """The fallback exists because a pipeline that never retries has no
    same-commit evidence at all -- but it is weaker, and reported separately."""
    no_retries = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", started_at="2026-07-20T10:00:00+00:00"),
            record(outcome=Outcome.FAIL, sha="b", started_at="2026-07-20T11:00:00+00:00"),
        ]
    )
    assert no_retries.retry_data_available is False
    assert flake_rate(no_retries) == intermittency_rate(no_retries)

    with_retries = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", execution_id=1),
            record(outcome=Outcome.FAIL, sha="a", attempt=2, execution_id=2),
        ]
    )
    assert flake_rate(with_retries) == divergence_rate(with_retries)


# --- windows ---------------------------------------------------------------


def test_shards_and_attempts_collapse_into_one_window_per_commit() -> None:
    """Eight rows for one commit is one observation of that commit, not eight."""
    records = [
        record(outcome=Outcome.PASS, sha="a", shard=str(index), execution_id=index + 1)
        for index in range(4)
    ]
    history = build_history(records)
    assert len(history.windows) == 1
    assert history.windows[0].observations == 4
    assert history.windows[0].status is WindowStatus.PASSED


def test_a_runner_asserted_rerun_is_divergence() -> None:
    """Surefire's <flakyFailure>: the runner already proved non-determinism."""
    history = build_history([record(outcome=Outcome.PASS, rerun=True)])
    assert history.windows[0].diverged is True


def test_cross_attempt_divergence_is_distinguished() -> None:
    across = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", attempt=1, execution_id=1),
            record(outcome=Outcome.FAIL, sha="a", attempt=2, execution_id=2),
        ]
    )
    assert across.windows[0].diverged_across_attempts is True

    within = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", attempt=1, shard="1", execution_id=1),
            record(outcome=Outcome.FAIL, sha="a", attempt=1, shard="2", execution_id=2),
        ]
    )
    assert within.windows[0].diverged is True
    assert within.windows[0].diverged_across_attempts is False


def test_failing_shards_are_recorded_as_an_order_dependency_hint() -> None:
    history = build_history(
        [
            record(outcome=Outcome.PASS, sha="a", shard="1", execution_id=1),
            record(outcome=Outcome.FAIL, sha="a", shard="3", execution_id=2),
        ]
    )
    assert history.windows[0].failing_shards == ("3",)


def test_skips_do_not_count_as_observations() -> None:
    """A test that did not run says nothing about whether it is flaky."""
    history = build_history([record(outcome=Outcome.SKIP)])
    assert history.observations == 0
    assert history.skipped == 1
    assert history.windows[0].status is WindowStatus.NO_DATA


def test_windows_are_chronological_oldest_first() -> None:
    history = build_history(
        [
            record(sha="late", started_at="2026-07-20T12:00:00+00:00", execution_id=2),
            record(sha="early", started_at="2026-07-20T10:00:00+00:00", execution_id=1),
        ]
    )
    assert [window.commit_sha for window in history.windows] == ["early", "late"]
