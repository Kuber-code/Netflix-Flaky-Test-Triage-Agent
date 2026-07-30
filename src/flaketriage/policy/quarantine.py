"""Quarantine policy: the deterministic decision.

**The LLM's classification is an input, never the decision.** That boundary is the
project's design position (ADR-0001) and this module is where it is cashed in: the
model contributes one field to a conjunction of four conditions, and the field it
contributes can only ever *veto* a quarantine, never cause one. A model that
hallucinated `RACE_CONDITION` on every test could not quarantine anything that the
flake rate had not already condemned.

Every recommendation is a conjunction. All four must hold:

1. Flake rate above ``policy.quarantine_flake_rate``.
2. At least ``policy.quarantine_min_observations`` observations.
3. The cause is neither `REAL_REGRESSION` nor `INFRA_FLAKE`.
4. No open quarantine already exists for the test.

Condition 3 is checked against **both** the detector's verdict and the model's
cause, because they can disagree and either one saying "regression" is sufficient
to refuse. Quarantining a real defect suppresses a signal somebody needs, and
quarantining an infrastructure failure blames a test author for a platform outage.

Every recommendation carries a TTL, an owner, and a de-quarantine condition. A
quarantine without an expiry becomes a graveyard of permanently disabled tests --
see ADR-0004.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from flaketriage.config import PolicyConfig
from flaketriage.detect.models import Detection, Verdict
from flaketriage.models import CauseCode, Classification, Frozen
from flaketriage.policy.ownership import (
    UNRESOLVED,
    CodeOwners,
    Owner,
    OwnerSource,
    resolve_owner,
)

#: Causes that must never be quarantined. A defect needs fixing, not silencing;
#: a platform failure is not the test's fault.
INELIGIBLE_CAUSES: Final = frozenset({CauseCode.REAL_REGRESSION, CauseCode.INFRA_FLAKE})

#: Detector verdicts that must never be quarantined, independently of any cause.
INELIGIBLE_VERDICTS: Final = frozenset({Verdict.REGRESSION, Verdict.NEW_FAILURE})


class QuarantineState(StrEnum):
    RECOMMENDED = "recommended"
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"

    @property
    def is_open(self) -> bool:
        return self in {QuarantineState.RECOMMENDED, QuarantineState.ACTIVE}


class RefusalReason(StrEnum):
    """Why a quarantine was not recommended. Always reported, never implied."""

    NONE = "none"
    FLAKE_RATE_BELOW_THRESHOLD = "flake_rate_below_threshold"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    CAUSE_IS_REGRESSION = "cause_is_regression"
    CAUSE_IS_INFRA = "cause_is_infra"
    VERDICT_INELIGIBLE = "verdict_ineligible"
    ALREADY_QUARANTINED = "already_quarantined"
    NOT_FLAKY = "not_flaky"


class QuarantineRecommendation(Frozen):
    """One deterministic decision, with everything it rests on.

    Refusals are first-class values rather than absences: "this test was not
    recommended, and here is which condition failed" is a useful thing to be able
    to show, and an empty list is not.
    """

    identity_id: int
    test: str
    recommended: bool
    refusal: RefusalReason = RefusalReason.NONE

    flake_rate: float = 0.0
    observations: int = 0
    cause: CauseCode = CauseCode.UNKNOWN
    verdict: Verdict = Verdict.HEALTHY

    owner: str = ""
    owner_source: OwnerSource = OwnerSource.UNRESOLVED
    ttl_days: int = 0
    expires_at: str = ""
    clean_runs_required: int = 0

    #: True when the history behind this decision was merged across an inferred
    #: rename, so the flake rate may not describe this test alone.
    merged_uncertain: bool = False

    @property
    def owner_is_a_guess(self) -> bool:
        return self.owner_source is OwnerSource.LAST_COMMITTER

    def summary(self) -> str:
        if not self.recommended:
            return f"not recommended ({self.refusal.value})"
        owner = self.owner or "unresolved"
        return (
            f"quarantine {self.test} until {self.expires_at[:10]} "
            f"(owner {owner}, releases after {self.clean_runs_required} clean runs)"
        )


def evaluate(
    detection: Detection,
    *,
    classification: Classification | None = None,
    already_quarantined: bool = False,
    config: PolicyConfig | None = None,
    codeowners: CodeOwners | None = None,
    repo_root: Path | None = None,
    allow_git: bool = True,
    now: datetime | None = None,
) -> QuarantineRecommendation:
    """Decide whether to recommend quarantining one test."""
    settings = config or PolicyConfig()
    cause = classification.cause if classification is not None else CauseCode.UNKNOWN
    moment = now or datetime.now(UTC)

    def build(
        *,
        recommended: bool,
        refusal: RefusalReason,
        owner: Owner | None = None,
        expires_at: str = "",
    ) -> QuarantineRecommendation:
        """Construct the result with the shared evidence fields filled in.

        Written out rather than splatted from a dict so the field types stay
        checkable -- a policy decision is the last place to want an untyped bag.
        """
        resolved = owner or UNRESOLVED
        return QuarantineRecommendation(
            identity_id=detection.identity_id,
            test=detection.identity.display_name,
            recommended=recommended,
            refusal=refusal,
            flake_rate=detection.flake_rate,
            observations=detection.observations,
            cause=cause,
            verdict=detection.verdict,
            merged_uncertain=detection.merged_uncertain,
            owner=resolved.name,
            owner_source=resolved.source,
            ttl_days=settings.quarantine_ttl_days if recommended else 0,
            expires_at=expires_at,
            clean_runs_required=settings.dequarantine_clean_runs if recommended else 0,
        )

    def refuse(reason: RefusalReason) -> QuarantineRecommendation:
        return build(recommended=False, refusal=reason)

    # Checked before anything else so that a regression can never reach the rest
    # of the conjunction, whichever layer identified it.
    if detection.verdict in INELIGIBLE_VERDICTS:
        return refuse(
            RefusalReason.CAUSE_IS_REGRESSION
            if detection.verdict is Verdict.REGRESSION
            else RefusalReason.VERDICT_INELIGIBLE
        )
    if cause is CauseCode.REAL_REGRESSION:
        return refuse(RefusalReason.CAUSE_IS_REGRESSION)
    if cause is CauseCode.INFRA_FLAKE:
        return refuse(RefusalReason.CAUSE_IS_INFRA)

    if detection.verdict is not Verdict.FLAKY:
        return refuse(RefusalReason.NOT_FLAKY)
    if already_quarantined:
        return refuse(RefusalReason.ALREADY_QUARANTINED)
    if detection.observations < settings.quarantine_min_observations:
        return refuse(RefusalReason.INSUFFICIENT_OBSERVATIONS)
    if detection.flake_rate <= settings.quarantine_flake_rate:
        return refuse(RefusalReason.FLAKE_RATE_BELOW_THRESHOLD)

    owner = resolve_owner(
        detection.identity.file_path or detection.identity.suite_path.split("::")[0],
        codeowners=codeowners,
        repo_root=repo_root,
        allow_git=allow_git,
    )
    expires = moment + timedelta(days=settings.quarantine_ttl_days)

    return build(
        recommended=True,
        refusal=RefusalReason.NONE,
        owner=owner,
        expires_at=expires.astimezone(UTC).isoformat(),
    )


def consecutive_clean_runs(outcomes: list[bool], *, since: str | None = None) -> int:
    """Trailing count of clean executions, newest-first input.

    ``since`` is accepted for symmetry with the store query that produces the
    list; filtering happens there, not here.
    """
    del since
    clean = 0
    for passed in outcomes:
        if not passed:
            break
        clean += 1
    return clean


def should_release(clean_runs: int, config: PolicyConfig | None = None) -> bool:
    """Whether enough clean executions have accumulated to return to blocking.

    Automatic rather than manual on purpose: a quarantine that only a human can
    lift is a quarantine nobody lifts.
    """
    settings = config or PolicyConfig()
    return clean_runs >= settings.dequarantine_clean_runs


def is_expiring(expires_at: str, *, within_days: int = 3, now: datetime | None = None) -> bool:
    """Whether a quarantine expires within ``within_days``.

    Surfaced in the report so expiry is a scheduled conversation rather than a
    surprise reopening of a red build.
    """
    moment = now or datetime.now(UTC)
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= moment + timedelta(days=within_days)


def is_expired(expires_at: str, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= moment
