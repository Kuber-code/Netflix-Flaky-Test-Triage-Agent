"""Group a test's executions into per-commit windows.

Every signal in §6.3 is a statement about outcomes *at a commit*, not about
outcomes in a list. A test with four shards and two attempts produces eight rows
for one commit; reading those as eight independent observations would make any
sharded suite look wildly unstable. So the raw execution rows are collapsed into
one window per commit SHA, and the signals are computed over windows.

Infrastructure failures are dropped here rather than filtered downstream, so that
no signal can accidentally see them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

from flaketriage.config import DetectConfig
from flaketriage.detect.infra import is_infra_failure
from flaketriage.models import Outcome
from flaketriage.store.repositories import ExecutionRecord


class WindowStatus(StrEnum):
    """The verdict for one commit's worth of observations."""

    PASSED = "passed"
    FAILED = "failed"
    DIVERGED = "diverged"
    NO_DATA = "no_data"


class ShaWindow:
    """All observations of one test at one commit SHA."""

    __slots__ = (
        "attempts",
        "branch",
        "commit_sha",
        "infra_excluded",
        "outcomes",
        "records",
        "rerun_observed",
        "shards",
        "started_at",
    )

    def __init__(self, commit_sha: str, records: Sequence[ExecutionRecord]) -> None:
        self.commit_sha = commit_sha
        self.records = tuple(records)
        self.started_at = min(record.started_at for record in records)
        self.branch = records[0].branch
        self.outcomes = tuple(
            record.outcome for record in records if record.outcome.counts_as_observation
        )
        self.attempts = tuple(sorted({record.attempt for record in records}))
        self.shards = tuple(sorted({record.shard_id for record in records if record.shard_id}))
        self.rerun_observed = any(record.rerun_observed for record in records)
        self.infra_excluded = 0

    @property
    def observations(self) -> int:
        return len(self.outcomes)

    @property
    def has_pass(self) -> bool:
        return any(outcome.is_pass for outcome in self.outcomes)

    @property
    def has_failure(self) -> bool:
        return any(outcome.is_failure for outcome in self.outcomes)

    @property
    def diverged(self) -> bool:
        """Both a pass and a failure at the same commit: a confirmed flake.

        A runner-asserted rerun counts too. Surefire's ``<flakyFailure>`` means
        the test failed and then passed inside one execution, which is the same
        fact reported by the runner instead of inferred from two rows.
        """
        return (self.has_pass and self.has_failure) or self.rerun_observed

    @property
    def diverged_across_attempts(self) -> bool:
        """Divergence where the differing outcomes came from different attempts.

        Called out separately because retry-driven divergence is the cleanest
        possible evidence: same code, same commit, only the attempt differs.
        """
        if not self.diverged or len(self.attempts) < 2:
            return False
        by_attempt: dict[int, set[Outcome]] = {}
        for record in self.records:
            if record.outcome.counts_as_observation:
                by_attempt.setdefault(record.attempt, set()).add(record.outcome)

        passing = {
            attempt
            for attempt, outcomes in by_attempt.items()
            if any(outcome.is_pass for outcome in outcomes)
        }
        failing = {
            attempt
            for attempt, outcomes in by_attempt.items()
            if any(outcome.is_failure for outcome in outcomes)
        }
        # Requires an attempt that only passed or one that only failed; if every
        # attempt was itself mixed, the divergence is within attempts, not across.
        return bool(passing and failing and (passing - failing or failing - passing))

    @property
    def status(self) -> WindowStatus:
        if self.diverged:
            return WindowStatus.DIVERGED
        if not self.outcomes:
            return WindowStatus.NO_DATA
        if self.has_failure:
            return WindowStatus.FAILED
        return WindowStatus.PASSED

    @property
    def failing_shards(self) -> tuple[str, ...]:
        """Shards in which this test failed. A failure confined to one shard is a
        hint at order dependency, which is why shard ids are carried this far."""
        return tuple(
            sorted(
                {
                    record.shard_id
                    for record in self.records
                    if record.shard_id and record.outcome.is_failure
                }
            )
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ShaWindow({self.commit_sha[:8]}, {self.status.value}, n={self.observations})"


class History:
    """A test's windows in chronological order, oldest first."""

    __slots__ = ("infra_excluded", "skipped", "windows")

    def __init__(self, windows: Sequence[ShaWindow], infra_excluded: int, skipped: int) -> None:
        self.windows = tuple(windows)
        self.infra_excluded = infra_excluded
        self.skipped = skipped

    @property
    def observations(self) -> int:
        """Total non-skip, non-infra observations across all windows."""
        return sum(window.observations for window in self.windows)

    @property
    def measurable_windows(self) -> tuple[ShaWindow, ...]:
        """Windows with at least two observations -- the only ones that *can* diverge.

        Used to answer "is same-commit divergence observable for this test at
        all?", which is a different question from what the flake rate divides by.
        A pipeline that never retries produces none of these, and its flake rate
        has to fall back to the weaker cross-commit measure. See
        :mod:`flaketriage.detect.rates`.
        """
        return tuple(window for window in self.windows if window.observations >= 2)

    @property
    def retry_data_available(self) -> bool:
        return bool(self.measurable_windows)

    @property
    def latest(self) -> ShaWindow | None:
        return self.windows[-1] if self.windows else None

    @property
    def latest_failure(self) -> ExecutionRecord | None:
        """The most recent failing execution, for evidence and classification."""
        for window in reversed(self.windows):
            failures = [record for record in window.records if record.outcome.is_failure]
            if failures:
                return failures[-1]
        return None

    def statuses(self) -> tuple[WindowStatus, ...]:
        return tuple(window.status for window in self.windows)


def build_history(
    records: Iterable[ExecutionRecord], config: DetectConfig | None = None
) -> History:
    """Collapse execution rows into chronological per-commit windows."""
    settings = config or DetectConfig()

    grouped: dict[str, list[ExecutionRecord]] = {}
    infra_excluded = 0
    skipped = 0

    for record in records:
        if is_infra_failure(record, settings):
            infra_excluded += 1
            continue
        if not record.outcome.counts_as_observation:
            skipped += 1
            # A skip is still evidence the test exists, so the window is kept --
            # it just contributes no observations. That is what stops a skipped
            # test from looking like a disappeared one to rename reconciliation.
        grouped.setdefault(record.commit_sha, []).append(record)

    windows = [ShaWindow(sha, group) for sha, group in grouped.items()]
    windows.sort(key=lambda window: (window.started_at, window.commit_sha))
    return History(windows=windows, infra_excluded=infra_excluded, skipped=skipped)
