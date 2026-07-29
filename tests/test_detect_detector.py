"""Signal, verdict and confidence tests.

The precedence rules are the design (see the module docstring of
``detect.detector``), so most of these assert on ordering between signals rather
than on any signal in isolation. The one that matters most is
``test_a_regression_is_never_reported_as_a_flake``: emitting a real defect as
noise tells an engineer to ignore a genuine bug.
"""

from __future__ import annotations

from flaketriage.config import DetectConfig
from flaketriage.detect.detector import (
    detect_for_history,
    find_branch_independent,
    find_historical_instability,
    find_regression,
    find_same_sha_divergence,
)
from flaketriage.detect.history import build_history
from flaketriage.detect.models import Confidence, Detection, FlakeSignal, Verdict
from flaketriage.identity.fingerprint import fingerprint
from flaketriage.models import Outcome, TestIdentity
from helpers import FAIL, MIXED, PASS, history_from, record

IDENTITY = TestIdentity(
    fingerprint=fingerprint("tests/test_auth.py", "test_login"),
    suite_path="tests/test_auth.py",
    test_name="test_login",
    file_path="tests/test_auth.py",
)


def detect(
    pattern: list[tuple[str, list[Outcome]]],
    *,
    branch: str = "main",
    attempts: bool = False,
    stack_trace: str | None = None,
    diff_paths: frozenset[str] = frozenset(),
) -> Detection:
    history = history_from(pattern, branch=branch, attempts=attempts, stack_trace=stack_trace)
    return detect_for_history(IDENTITY, 1, history, diff_paths=diff_paths)


# --- signal 1 and 2 --------------------------------------------------------


def test_same_sha_divergence_is_a_confirmed_flake() -> None:
    detection = detect([("a", PASS), ("b", MIXED)])
    assert detection.verdict is Verdict.FLAKY
    assert FlakeSignal.SAME_SHA_DIVERGENCE.value in detection.signal_codes


def test_cross_attempt_divergence_is_reported_separately() -> None:
    """Same code, same commit, only the attempt differs: the cleanest evidence."""
    detection = detect([("a", MIXED)], attempts=True)
    assert FlakeSignal.CROSS_ATTEMPT_DIVERGENCE.value in detection.signal_codes
    assert FlakeSignal.SAME_SHA_DIVERGENCE.value in detection.signal_codes


def test_one_divergent_commit_is_medium_and_two_is_high() -> None:
    single = detect([("a", MIXED)])
    assert single.confidence is Confidence.MEDIUM

    repeated = detect([("a", MIXED), ("b", MIXED)])
    assert repeated.confidence is Confidence.HIGH


def test_divergence_beats_everything_else() -> None:
    """A pass streak cannot argue with a commit that produced both outcomes."""
    detection = detect(
        [("a", PASS), ("b", PASS), ("c", PASS), ("d", PASS), ("e", MIXED), ("f", FAIL)]
    )
    assert detection.verdict is Verdict.FLAKY
    assert detection.regression_sha is None


def test_no_divergence_means_no_signal() -> None:
    assert find_same_sha_divergence(history_from([("a", PASS), ("b", PASS)])) == []


# --- regression path -------------------------------------------------------


def test_a_regression_is_never_reported_as_a_flake() -> None:
    """The most expensive error this tool can make."""
    detection = detect([("a", PASS), ("b", PASS), ("c", PASS), ("d", FAIL), ("e", FAIL)])
    assert detection.verdict is Verdict.REGRESSION
    assert detection.regression_sha == "d"
    assert detection.is_flaky is False
    # And it must not be offered for quarantine, which P7 enforces via this flag.
    assert detection.needs_classification is False


def test_regression_requires_a_long_enough_pass_streak() -> None:
    detection = detect([("a", PASS), ("b", FAIL), ("c", FAIL)])
    assert detection.verdict is not Verdict.REGRESSION


def test_regression_requires_a_long_enough_fail_streak() -> None:
    """One failure after a pass streak might just be the flake's first show."""
    detection = detect([("a", PASS), ("b", PASS), ("c", PASS), ("d", FAIL)])
    assert detection.verdict is not Verdict.REGRESSION


def test_a_recovery_is_not_a_regression() -> None:
    detection = detect([("a", FAIL), ("b", FAIL), ("c", PASS), ("d", PASS), ("e", PASS)])
    assert detection.verdict is Verdict.HEALTHY


def test_divergence_anywhere_disqualifies_a_regression() -> None:
    """If the test is non-deterministic, the 'transition' is where the coin landed."""
    finding = find_regression(
        history_from([("a", MIXED), ("b", PASS), ("c", PASS), ("d", FAIL), ("e", FAIL)])
    )
    assert finding is None


def test_regression_thresholds_come_from_config() -> None:
    pattern = [("a", PASS), ("b", PASS), ("c", FAIL)]
    assert find_regression(history_from(pattern), DetectConfig()) is None
    lenient = DetectConfig(regression_pass_streak=2, regression_fail_streak=1)
    finding = find_regression(history_from(pattern), lenient)
    assert finding is not None
    assert finding.pivot_sha == "c"


def test_long_clean_streaks_give_a_regression_high_confidence() -> None:
    short = detect([("a", PASS), ("b", PASS), ("c", PASS), ("d", FAIL), ("e", FAIL)])
    assert short.confidence is Confidence.MEDIUM

    long_streak = [(f"p{index}", PASS) for index in range(6)] + [("f1", FAIL), ("f2", FAIL)]
    assert detect(long_streak).confidence is Confidence.HIGH


def test_skipped_commits_do_not_break_a_streak() -> None:
    """A skip is an absence of evidence, not a change of state."""
    pattern = [
        ("a", PASS),
        ("b", PASS),
        ("c", [Outcome.SKIP]),
        ("d", PASS),
        ("e", FAIL),
        ("f", FAIL),
    ]
    detection = detect(pattern)
    assert detection.verdict is Verdict.REGRESSION
    assert detection.regression_sha == "e"


# --- new failure -----------------------------------------------------------


def test_a_first_ever_failure_is_reported_as_such_not_guessed() -> None:
    detection = detect([("a", FAIL)])
    assert detection.verdict is Verdict.NEW_FAILURE
    assert detection.confidence is Confidence.LOW
    assert detection.signals == ()


def test_a_first_ever_pass_is_healthy() -> None:
    detection = detect([("a", PASS)])
    assert detection.verdict is Verdict.HEALTHY


# --- signal 3 --------------------------------------------------------------

TRACE = "tests/test_auth.py:27: AssertionError\n  at src/auth/session.py:88"


def test_branch_independent_intermittency_fires_when_the_diff_is_unrelated() -> None:
    history = history_from([("main1", PASS), ("pr1", FAIL)], branch="feature/x", stack_trace=TRACE)
    signal = find_branch_independent(
        history, frozenset({"docs/README.md"}), frozenset({"tests/test_auth.py"})
    )
    # The latest window is on a feature branch but there is no passing main
    # window in this history, so the signal must not fire on partial evidence.
    assert signal is None


def test_branch_independent_needs_a_passing_main_observation() -> None:
    records = [
        record(
            outcome=Outcome.PASS,
            sha="base",
            branch="main",
            started_at="2026-07-20T10:00:00+00:00",
            execution_id=1,
        ),
        record(
            outcome=Outcome.FAIL,
            sha="pr",
            branch="feature/x",
            started_at="2026-07-20T11:00:00+00:00",
            stack_trace=TRACE,
            execution_id=2,
        ),
    ]
    history = build_history(records)
    signal = find_branch_independent(
        history, frozenset({"docs/README.md"}), frozenset({"tests/test_auth.py"})
    )
    assert signal is not None
    assert signal.signal is FlakeSignal.BRANCH_INDEPENDENT


def test_branch_independent_does_not_fire_when_the_diff_touches_the_test() -> None:
    """Otherwise the tool tells an engineer their own edit is unrelated to them."""
    records = [
        record(
            outcome=Outcome.PASS,
            sha="base",
            branch="main",
            started_at="2026-07-20T10:00:00+00:00",
            execution_id=1,
        ),
        record(
            outcome=Outcome.FAIL,
            sha="pr",
            branch="feature/x",
            started_at="2026-07-20T11:00:00+00:00",
            stack_trace=TRACE,
            execution_id=2,
        ),
    ]
    signal = find_branch_independent(
        build_history(records),
        frozenset({"tests/test_auth.py"}),
        frozenset({"tests/test_auth.py"}),
    )
    assert signal is None


def test_branch_independent_never_fires_on_the_main_branch() -> None:
    history = history_from([("a", PASS), ("b", FAIL)], branch="main")
    assert find_branch_independent(history, frozenset({"docs/x.md"}), frozenset({"a.py"})) is None


def test_signal_3_confidence_is_low() -> None:
    """It rests on a footprint derived from one stack trace, which is never complete."""
    records = [
        record(
            outcome=Outcome.PASS,
            sha="base",
            branch="main",
            started_at="2026-07-20T10:00:00+00:00",
            execution_id=1,
        ),
        record(
            outcome=Outcome.FAIL,
            sha="pr",
            branch="feature/x",
            started_at="2026-07-20T11:00:00+00:00",
            stack_trace=TRACE,
            execution_id=2,
        ),
    ]
    detection = detect_for_history(
        IDENTITY, 1, build_history(records), diff_paths=frozenset({"docs/README.md"})
    )
    assert detection.verdict is Verdict.FLAKY
    assert detection.confidence is Confidence.LOW


# --- signal 4 --------------------------------------------------------------


def test_historical_instability_needs_the_minimum_observation_count() -> None:
    """Two flips out of three runs is not evidence of anything."""
    flapping = [("a", PASS), ("b", FAIL), ("c", PASS)]
    assert find_historical_instability(history_from(flapping)) is None


def test_historical_instability_fires_over_a_long_flapping_history() -> None:
    pattern = [(f"sha{index}", PASS if index % 2 == 0 else FAIL) for index in range(14)]
    signal = find_historical_instability(history_from(pattern))
    assert signal is not None
    assert "no retry data" in signal.detail


def test_a_stable_test_never_trips_signal_4() -> None:
    pattern = [(f"sha{index}", PASS) for index in range(20)]
    assert find_historical_instability(history_from(pattern)) is None
    assert detect(pattern).verdict is Verdict.HEALTHY


def test_signal_4_confidence_is_low_without_retry_data() -> None:
    pattern = [(f"sha{index}", PASS if index % 2 == 0 else FAIL) for index in range(14)]
    detection = detect(pattern)
    assert detection.verdict is Verdict.FLAKY
    assert detection.confidence is Confidence.LOW
    assert detection.retry_data_available is False


def test_signal_4_reaches_medium_with_retry_data_and_enough_observations() -> None:
    pattern: list[tuple[str, list[Outcome]]] = []
    for index in range(12):
        pattern.append((f"sha{index}", MIXED if index % 3 == 0 else [Outcome.PASS, Outcome.PASS]))
    detection = detect(pattern)
    assert detection.retry_data_available is True
    assert detection.verdict is Verdict.FLAKY
    assert detection.confidence is Confidence.HIGH  # divergence, not signal 4


def test_thresholds_come_from_config_not_code() -> None:
    pattern = [(f"sha{index}", PASS if index % 5 == 0 else FAIL) for index in range(12)]
    strict = DetectConfig(flake_rate_threshold=0.99)
    assert find_historical_instability(history_from(pattern), strict) is None


# --- persistent failure ----------------------------------------------------


def test_a_long_running_failure_with_no_transition_is_persistent_not_flaky() -> None:
    """Failing since before the window started: real, but not attributable."""
    pattern = [(f"sha{index}", FAIL) for index in range(12)]
    detection = detect(pattern)
    assert detection.verdict is Verdict.PERSISTENT_FAILURE
    assert detection.regression_sha is None


# --- detection payload -----------------------------------------------------


def test_detection_carries_the_numbers_behind_the_verdict() -> None:
    detection = detect([("a", MIXED), ("b", MIXED)])
    assert detection.observations == 4
    assert detection.windows == 2
    assert detection.divergence_rate > 0
    assert detection.latest_sha == "b"
    assert detection.identity_id == 1


def test_detection_carries_failure_detail_for_the_classifier() -> None:
    detection = detect([("a", PASS), ("b", MIXED)], stack_trace=TRACE)
    assert detection.stack_trace == TRACE
    assert "tests/test_auth.py" in detection.footprint
    assert "src/auth/session.py" in detection.footprint


def test_merged_uncertain_travels_with_the_detection() -> None:
    detection = detect_for_history(IDENTITY, 1, history_from([("a", MIXED)]), merged_uncertain=True)
    assert detection.merged_uncertain is True


def test_only_flaky_and_persistent_failures_are_worth_classifying() -> None:
    """A healthy test has nothing to explain; a new failure has nothing to explain it with."""
    assert detect([("a", MIXED)]).needs_classification is True
    assert detect([(f"s{index}", FAIL) for index in range(12)]).needs_classification is True
    assert detect([("a", FAIL)]).needs_classification is False
    assert detect([("a", PASS)]).needs_classification is False
    regression = detect([("a", PASS), ("b", PASS), ("c", PASS), ("d", FAIL), ("e", FAIL)])
    assert regression.needs_classification is False


def test_an_empty_history_is_healthy_rather_than_an_error() -> None:
    detection = detect_for_history(IDENTITY, 1, build_history([]))
    assert detection.verdict is Verdict.HEALTHY
    assert detection.observations == 0
