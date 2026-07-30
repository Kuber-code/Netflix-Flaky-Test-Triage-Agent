"""Policy engine: deterministic quarantine rules, TTL, ownership.

Consumes classifier output as evidence only. The model contributes one field to a
conjunction of four conditions, and that field can only ever veto a quarantine --
never cause one. See ADR-0001 and ADR-0004.
"""

from flaketriage.policy.ownership import (
    CodeOwners,
    Owner,
    OwnerSource,
    last_committer,
    resolve_owner,
)
from flaketriage.policy.quarantine import (
    INELIGIBLE_CAUSES,
    INELIGIBLE_VERDICTS,
    QuarantineRecommendation,
    QuarantineState,
    RefusalReason,
    consecutive_clean_runs,
    evaluate,
    is_expired,
    is_expiring,
    should_release,
)
from flaketriage.policy.records import (
    QuarantineRecord,
    close,
    expire_overdue,
    list_quarantines,
    open_quarantine_ids,
    record_recommendation,
    summarize_states,
)

__all__ = [
    "INELIGIBLE_CAUSES",
    "INELIGIBLE_VERDICTS",
    "CodeOwners",
    "Owner",
    "OwnerSource",
    "QuarantineRecommendation",
    "QuarantineRecord",
    "QuarantineState",
    "RefusalReason",
    "close",
    "consecutive_clean_runs",
    "evaluate",
    "expire_overdue",
    "is_expired",
    "is_expiring",
    "last_committer",
    "list_quarantines",
    "open_quarantine_ids",
    "record_recommendation",
    "resolve_owner",
    "should_release",
    "summarize_states",
]
