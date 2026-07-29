"""Wire rename detection into ingest.

Kept separate from :mod:`flaketriage.identity.alias` so that the matching rules
stay pure and testable without a database, and so the store-touching part is one
short, auditable function.
"""

from __future__ import annotations

from flaketriage.config import IdentityConfig
from flaketriage.identity.alias import AliasCandidate, detect_renames
from flaketriage.models import TestIdentity
from flaketriage.obs import get_logger
from flaketriage.store.repositories import RunStore

log = get_logger(__name__)


class ReconcileResult:
    """Aliases recorded for one run."""

    __slots__ = ("candidates",)

    def __init__(self, candidates: tuple[AliasCandidate, ...]) -> None:
        self.candidates = candidates

    @property
    def recorded(self) -> int:
        return len(self.candidates)

    @property
    def uncertain(self) -> int:
        return sum(1 for candidate in self.candidates if not candidate.certain)


def reconcile_renames(
    store: RunStore, run_pk: int, config: IdentityConfig | None = None
) -> ReconcileResult:
    """Detect and record renames between this run and everything before it.

    "Disappeared" means an identity that has history but produced no execution in
    this run. "Appeared" means an identity in this run with no prior history. A
    test that is merely skipped in one run does not disappear, because a skip is
    still an execution row.
    """
    settings = config or IdentityConfig()

    current_ids = set(store.identity_ids_for_run(run_pk))
    if not current_ids:
        return ReconcileResult(())

    previous = store.identities_before_run(run_pk)
    previous_ids = {identity_id for identity_id, _ in previous}

    disappeared = [identity for identity_id, identity in previous if identity_id not in current_ids]
    appeared = [
        identity
        for identity_id in sorted(current_ids - previous_ids)
        if (identity := store.identity_by_id(identity_id)) is not None
    ]

    candidates = detect_renames(disappeared, appeared, settings)
    if not candidates:
        return ReconcileResult(())

    recorded: list[AliasCandidate] = []
    for candidate in candidates:
        old_id = store.identity_id_by_fingerprint(candidate.old_fingerprint)
        new_id = store.identity_id_by_fingerprint(candidate.new_fingerprint)
        if old_id is None or new_id is None:  # pragma: no cover - defensive
            continue
        store.record_alias(
            old_id,
            new_id,
            similarity=candidate.similarity,
            certain=candidate.certain,
            run_pk=run_pk,
        )
        recorded.append(candidate)
        log.info(
            "identity_alias_recorded",
            run_pk=run_pk,
            old_identity_id=old_id,
            new_identity_id=new_id,
            distance=round(candidate.distance, 4),
            certain=candidate.certain,
        )

    return ReconcileResult(tuple(recorded))


def describe(candidate: AliasCandidate, old: TestIdentity, new: TestIdentity) -> str:
    """One-line rendering for reports. Uncertain merges are labelled as such."""
    marker = "" if candidate.certain else " (merged_uncertain)"
    return f"{old.display_name} -> {new.display_name}{marker}"
