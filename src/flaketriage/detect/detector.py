"""The deterministic detector.

**Precedence is the whole design.** The signals are not combined into a score;
they are consulted in a fixed order, because they are not commensurable:

1. **Same-commit divergence** wins outright. If one commit produced both a pass
   and a failure, the test is non-deterministic. Nothing else can override an
   observation that direct -- not a pass streak, not a clean-looking transition.
2. **Regression** comes next. A clean pass streak followed by a clean fail streak
   pivoting on one commit is a real defect. Emitting it as a flake is the most
   expensive error this tool can make, because it tells an engineer to ignore a
   genuine bug, so regression is checked *before* the historical-instability
   signal that a transition would otherwise trip.
3. **New failure** is reported as such and never guessed. A test failing on its
   first observed execution has no history; "insufficient evidence" is the
   correct answer, not the absence of one.
4. **Weaker signals** -- branch-independent intermittency and historical
   instability -- fire last, at reduced confidence.

Confidence is derived from which signals fired and how much data backs them, and
is always emitted. A bare boolean would hide the difference between "diverged
twice at the same commit yesterday" and "the flip rate crept over five percent".
"""

from __future__ import annotations

from flaketriage.config import DetectConfig
from flaketriage.detect.footprint import diff_touches_footprint, footprint
from flaketriage.detect.history import History, ShaWindow, WindowStatus, build_history
from flaketriage.detect.models import (
    Confidence,
    Detection,
    FlakeSignal,
    SignalEvidence,
    Verdict,
)
from flaketriage.detect.rates import divergence_rate, flake_rate, intermittency_rate
from flaketriage.models import TestIdentity
from flaketriage.obs import get_logger
from flaketriage.store.repositories import ExecutionRecord, RunStore

log = get_logger(__name__)


class RegressionFinding:
    """A clean pass-to-fail transition and the commit it pivots on."""

    __slots__ = ("fail_streak", "pass_streak", "pivot_sha")

    def __init__(self, pivot_sha: str, pass_streak: int, fail_streak: int) -> None:
        self.pivot_sha = pivot_sha
        self.pass_streak = pass_streak
        self.fail_streak = fail_streak


def find_same_sha_divergence(history: History) -> list[SignalEvidence]:
    """Signals 1 and 2. Reported separately because their evidence differs."""
    diverged = [window for window in history.windows if window.diverged]
    if not diverged:
        return []

    evidence = [
        SignalEvidence(
            signal=FlakeSignal.SAME_SHA_DIVERGENCE,
            detail=(
                f"{len(diverged)} commit(s) produced both a pass and a failure; "
                f"most recent: {diverged[-1].commit_sha[:12]}"
            ),
            observations=sum(window.observations for window in diverged),
        )
    ]

    across_attempts = [window for window in diverged if window.diverged_across_attempts]
    if across_attempts:
        evidence.append(
            SignalEvidence(
                signal=FlakeSignal.CROSS_ATTEMPT_DIVERGENCE,
                detail=(
                    f"outcome differed between attempts at {across_attempts[-1].commit_sha[:12]} "
                    f"(attempts {', '.join(str(a) for a in across_attempts[-1].attempts)})"
                ),
                observations=sum(window.observations for window in across_attempts),
            )
        )
    return evidence


def find_regression(
    history: History, config: DetectConfig | None = None
) -> RegressionFinding | None:
    """A consistent pass streak turning into a consistent fail streak at one commit.

    Requires the transition to be clean: no divergent window anywhere in the
    history, because divergence means the test is non-deterministic and the
    "transition" is just where the coin happened to land.
    """
    settings = config or DetectConfig()
    windows = [window for window in history.windows if window.status is not WindowStatus.NO_DATA]
    if any(window.status is WindowStatus.DIVERGED for window in windows):
        return None

    trailing_failures = 0
    for window in reversed(windows):
        if window.status is not WindowStatus.FAILED:
            break
        trailing_failures += 1

    if trailing_failures < settings.regression_fail_streak:
        return None

    preceding = windows[: len(windows) - trailing_failures]
    leading_passes = 0
    for window in reversed(preceding):
        if window.status is not WindowStatus.PASSED:
            break
        leading_passes += 1

    if leading_passes < settings.regression_pass_streak:
        return None

    pivot = windows[len(windows) - trailing_failures]
    return RegressionFinding(
        pivot_sha=pivot.commit_sha,
        pass_streak=leading_passes,
        fail_streak=trailing_failures,
    )


def find_branch_independent(
    history: History,
    diff_paths: frozenset[str],
    test_footprint: frozenset[str],
    config: DetectConfig | None = None,
) -> SignalEvidence | None:
    """Signal 3: fails on a branch whose change cannot plausibly have caused it.

    Weak by construction. It rests on the footprint being complete, and a
    footprint derived from one stack trace never is -- the test may exercise code
    it did not happen to fail inside. Hence low confidence and an explicit note.
    """
    settings = config or DetectConfig()
    latest = history.latest
    if latest is None or not latest.has_failure:
        return None
    if latest.branch in {settings.main_branch, ""}:
        return None
    if not diff_paths or not test_footprint:
        return None
    if diff_touches_footprint(diff_paths, test_footprint):
        return None

    passed_on_main = [
        window
        for window in history.windows
        if window.branch == settings.main_branch and window.status is WindowStatus.PASSED
    ]
    if not passed_on_main:
        return None

    return SignalEvidence(
        signal=FlakeSignal.BRANCH_INDEPENDENT,
        detail=(
            f"fails on {latest.branch} whose diff touches none of "
            f"{len(test_footprint)} file(s) this test exercises, and passes on "
            f"{settings.main_branch}"
        ),
        observations=len(passed_on_main),
    )


def find_historical_instability(
    history: History, config: DetectConfig | None = None
) -> SignalEvidence | None:
    """Signal 4: the smoothed flake rate clears the threshold."""
    settings = config or DetectConfig()
    if history.observations < settings.min_observations:
        return None

    rate = flake_rate(history, settings)
    if rate <= settings.flake_rate_threshold:
        return None

    basis = (
        "same-commit divergence"
        if history.retry_data_available
        else "outcome flips between commits (no retry data in this pipeline)"
    )
    return SignalEvidence(
        signal=FlakeSignal.HISTORICAL_INSTABILITY,
        detail=(
            f"flake rate {rate:.1%} over {history.observations} observations "
            f"exceeds {settings.flake_rate_threshold:.1%}, measured from {basis}"
        ),
        observations=history.observations,
    )


def _confidence(
    signals: list[SignalEvidence], history: History, config: DetectConfig
) -> Confidence:
    """Confidence follows the strongest signal, tempered by how much data backs it."""
    codes = {evidence.signal for evidence in signals}

    if FlakeSignal.SAME_SHA_DIVERGENCE in codes or FlakeSignal.CROSS_ATTEMPT_DIVERGENCE in codes:
        diverged = sum(1 for window in history.windows if window.diverged)
        # One divergent commit is a real observation but a small sample; two or
        # more is a pattern.
        return Confidence.HIGH if diverged >= 2 else Confidence.MEDIUM

    if FlakeSignal.HISTORICAL_INSTABILITY in codes:
        if history.retry_data_available and history.observations >= config.min_observations * 2:
            return Confidence.MEDIUM
        return Confidence.LOW

    return Confidence.LOW


def detect_for_history(
    identity: TestIdentity,
    identity_id: int,
    history: History,
    *,
    diff_paths: frozenset[str] = frozenset(),
    merged_uncertain: bool = False,
    config: DetectConfig | None = None,
) -> Detection:
    """Run every signal over one test's history and pick a verdict."""
    settings = config or DetectConfig()

    latest = history.latest
    latest_failure = history.latest_failure
    test_footprint = footprint(
        test_file=identity.file_path or identity.suite_path.split("::")[0],
        stack_trace=latest_failure.stack_trace if latest_failure else None,
        failure_message=latest_failure.failure_message if latest_failure else None,
    )

    signals = find_same_sha_divergence(history)
    regression = find_regression(history, settings) if not signals else None

    if regression is None:
        branch_signal = find_branch_independent(history, diff_paths, test_footprint, settings)
        if branch_signal is not None:
            signals.append(branch_signal)
        instability = find_historical_instability(history, settings)
        if instability is not None:
            signals.append(instability)

    verdict, confidence = _decide(history, signals, regression, settings)

    return Detection(
        identity=identity,
        identity_id=identity_id,
        verdict=verdict,
        confidence=confidence,
        signals=tuple(signals),
        flake_rate=flake_rate(history, settings),
        divergence_rate=divergence_rate(history, settings),
        intermittency_rate=intermittency_rate(history, settings),
        retry_data_available=history.retry_data_available,
        observations=history.observations,
        windows=len(history.windows),
        infra_excluded=history.infra_excluded,
        latest_outcome=latest_failure.outcome
        if latest_failure is not None and latest is not None and latest.has_failure
        else (latest.outcomes[-1] if latest and latest.outcomes else None),
        latest_sha=latest.commit_sha if latest else None,
        regression_sha=regression.pivot_sha if regression else None,
        failure_message=latest_failure.failure_message if latest_failure else None,
        failure_type=latest_failure.failure_type if latest_failure else None,
        stack_trace=latest_failure.stack_trace if latest_failure else None,
        footprint=tuple(sorted(test_footprint)),
        failing_shards=_failing_shards(latest),
        merged_uncertain=merged_uncertain,
    )


def _failing_shards(latest: ShaWindow | None) -> tuple[str, ...]:
    return latest.failing_shards if latest is not None else ()


def _decide(
    history: History,
    signals: list[SignalEvidence],
    regression: RegressionFinding | None,
    config: DetectConfig,
) -> tuple[Verdict, Confidence]:
    """Apply the precedence order documented at the top of this module."""
    if signals and signals[0].signal in {
        FlakeSignal.SAME_SHA_DIVERGENCE,
        FlakeSignal.CROSS_ATTEMPT_DIVERGENCE,
    }:
        return Verdict.FLAKY, _confidence(signals, history, config)

    if regression is not None:
        # Long, clean streaks on both sides leave little room for another reading.
        clean = (
            regression.pass_streak >= config.regression_pass_streak * 2
            and regression.fail_streak >= config.regression_fail_streak
        )
        return Verdict.REGRESSION, Confidence.HIGH if clean else Confidence.MEDIUM

    latest = history.latest
    if latest is None or not latest.has_failure:
        return Verdict.HEALTHY, Confidence.HIGH

    if signals:
        return Verdict.FLAKY, _confidence(signals, history, config)

    if len(history.windows) <= 1:
        return Verdict.NEW_FAILURE, Confidence.LOW

    if history.observations < config.min_observations:
        return Verdict.PERSISTENT_FAILURE, Confidence.LOW
    return Verdict.PERSISTENT_FAILURE, Confidence.MEDIUM


def detect_all(
    store: RunStore,
    *,
    config: DetectConfig | None = None,
    identity_ids: list[int] | None = None,
    since: str | None = None,
) -> list[Detection]:
    """Detect over every known test, or over ``identity_ids`` if given.

    Aliased identities are collapsed to one detection per logical test, so a
    renamed test is reported once with its merged history rather than twice with
    two truncated ones.
    """
    settings = config or DetectConfig()
    candidates = identity_ids if identity_ids is not None else store.all_identity_ids()

    detections: list[Detection] = []
    reported: set[int] = set()

    for identity_id in candidates:
        if identity_id in reported:
            continue
        group = store.identity_group(identity_id)
        reported.update(group.identity_ids)

        # Report against the most recently seen member of the group: after a
        # rename, the current name is the one an engineer will recognize.
        primary_id = max(group.identity_ids)
        identity = store.identity_by_id(primary_id)
        if identity is None:  # pragma: no cover - defensive
            continue

        records = store.executions_for_group(identity_id, limit=settings.window_executions)
        if since is not None:
            records = [record for record in records if record.started_at >= since]
        if not records:
            continue

        history = build_history(records, settings)
        diff_paths = _diff_paths_for_latest(store, history)

        detections.append(
            detect_for_history(
                identity,
                primary_id,
                history,
                diff_paths=diff_paths,
                merged_uncertain=group.merged_uncertain,
                config=settings,
            )
        )

    detections.sort(key=lambda detection: (-detection.flake_rate, detection.identity.display_name))
    log.info(
        "detect_complete",
        tests_examined=len(reported),
        detections=len(detections),
        flaky=sum(1 for detection in detections if detection.verdict is Verdict.FLAKY),
        regressions=sum(1 for d in detections if d.verdict is Verdict.REGRESSION),
    )
    return detections


def _diff_paths_for_latest(store: RunStore, history: History) -> frozenset[str]:
    latest = history.latest
    if latest is None:
        return frozenset()
    return store.diff_paths_for_sha(latest.commit_sha)


def latest_failing_record(history: History) -> ExecutionRecord | None:
    """Convenience re-export for the classifier, which needs the same record."""
    return history.latest_failure
