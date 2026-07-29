"""Detector output types.

Everything here is produced without a model. See ADR-0001: whether a test *is*
flaky is a question about observed outcomes, answered by counting.
"""

from __future__ import annotations

from enum import StrEnum

from flaketriage.models import Frozen, Outcome, TestIdentity


class FlakeSignal(StrEnum):
    """The four signals of §6.3, in descending order of evidence strength."""

    SAME_SHA_DIVERGENCE = "same_sha_divergence"
    CROSS_ATTEMPT_DIVERGENCE = "cross_attempt_divergence"
    BRANCH_INDEPENDENT = "branch_independent_intermittency"
    HISTORICAL_INSTABILITY = "historical_instability"


class Verdict(StrEnum):
    """What the detector concluded. Never a bare boolean."""

    FLAKY = "flaky"
    REGRESSION = "regression"
    NEW_FAILURE = "new_failure"
    PERSISTENT_FAILURE = "persistent_failure"
    HEALTHY = "healthy"

    @property
    def is_failure_state(self) -> bool:
        return self is not Verdict.HEALTHY


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalEvidence(Frozen):
    """One signal that fired, with the observation that made it fire."""

    signal: FlakeSignal
    detail: str
    observations: int = 0


class Detection(Frozen):
    """The deterministic verdict for one logical test.

    Carries the numbers the verdict rests on, not just the verdict, so that a
    reader can disagree with the thresholds without re-running anything.
    """

    identity: TestIdentity
    identity_id: int
    verdict: Verdict
    confidence: Confidence
    signals: tuple[SignalEvidence, ...] = ()

    flake_rate: float = 0.0
    divergence_rate: float = 0.0
    intermittency_rate: float = 0.0
    retry_data_available: bool = False

    observations: int = 0
    windows: int = 0
    infra_excluded: int = 0

    latest_outcome: Outcome | None = None
    latest_sha: str | None = None
    regression_sha: str | None = None

    failure_message: str | None = None
    failure_type: str | None = None
    stack_trace: str | None = None
    footprint: tuple[str, ...] = ()
    failing_shards: tuple[str, ...] = ()

    # True when this test's history was stitched together across a rename that
    # was inferred rather than observed. See ADR-0002.
    merged_uncertain: bool = False

    @property
    def signal_codes(self) -> tuple[str, ...]:
        return tuple(evidence.signal.value for evidence in self.signals)

    @property
    def is_flaky(self) -> bool:
        return self.verdict is Verdict.FLAKY

    @property
    def needs_classification(self) -> bool:
        """Whether it is worth spending a model call on this test.

        A healthy test has nothing to explain, and a new failure has no evidence
        to explain it with -- classifying either would be paying for a guess.
        """
        return self.verdict in {Verdict.FLAKY, Verdict.PERSISTENT_FAILURE}

    @property
    def priority(self) -> tuple[float, int]:
        """Sort key for batch bounding: flake rate first, then recency of evidence."""
        return (self.flake_rate, self.observations)
