"""Rename and move tolerance for test identities.

A test's history is keyed on its fingerprint, and the fingerprint changes when
the test is renamed or its file is moved. Without reconciliation, every rename
resets a test's flake rate to zero -- which is exactly the moment an engineer is
most likely to be touching a flaky test.

**The rule.** When an identity disappears from a run and a previously unseen one
appears in the same run, they are candidates for the same logical test. A
candidate is accepted when:

1. Parameters match exactly. ``test_login[user=a]`` and ``test_login[user=b]``
   are different instances, never a rename of each other.
2. The combined distance -- half the name distance plus half the suite-path
   distance -- is within ``identity.alias_max_distance``. Weighting both halves
   means a pure rename (path unchanged) and a pure move (name unchanged) each
   clear the bar, while a test that was renamed *and* moved simultaneously does
   not. That case is genuinely ambiguous and is refused rather than guessed.
3. The pairing is unambiguous. If two disappeared tests compete for one
   appeared test at the same distance, neither is merged: with two equally good
   explanations there is no evidence to choose between them.

**Exposing the uncertainty.** Only matches within
``identity.alias_certain_distance`` are recorded as certain. Everything else is
stored with ``certain = 0`` and surfaced downstream as ``merged_uncertain``.
Silently merging two tests' histories would produce a flake rate that is not
about any real test, and the reader would have no way to know. See ADR-0002.
"""

from __future__ import annotations

from collections import Counter
from itertools import groupby
from typing import Final

from flaketriage.config import IdentityConfig
from flaketriage.identity.similarity import normalized_distance
from flaketriage.models import Frozen, TestIdentity
from flaketriage.obs import get_logger

log = get_logger(__name__)

# Name and path contribute equally. See rule 2 above for why the split matters.
_NAME_WEIGHT: Final = 0.5
_PATH_WEIGHT: Final = 0.5


class AliasCandidate(Frozen):
    """A proposed merge of two identities into one logical test."""

    old_fingerprint: str
    new_fingerprint: str
    distance: float
    certain: bool

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


def combined_distance(old: TestIdentity, new: TestIdentity) -> float:
    """Distance between two identities on name and suite path together."""
    return _NAME_WEIGHT * normalized_distance(
        old.test_name, new.test_name
    ) + _PATH_WEIGHT * normalized_distance(old.suite_path, new.suite_path)


def detect_renames(
    disappeared: list[TestIdentity],
    appeared: list[TestIdentity],
    config: IdentityConfig | None = None,
) -> list[AliasCandidate]:
    """Pair identities that vanished with identities that showed up.

    Returns candidates in ascending distance order. Identities that could not be
    paired unambiguously are simply absent from the result: a test with no alias
    starts a fresh history, which is the safe default.
    """
    settings = config or IdentityConfig()
    if not disappeared or not appeared:
        return []

    scored: list[_Pair] = []
    for old in disappeared:
        for new in appeared:
            if old.parameters != new.parameters:
                continue
            if old.fingerprint == new.fingerprint:  # pragma: no cover - defensive
                continue
            distance = combined_distance(old, new)
            if distance <= settings.alias_max_distance:
                scored.append(_Pair(distance, old, new))

    scored.sort(key=lambda pair: (pair.rank, pair.old.fingerprint, pair.new.fingerprint))

    accepted: list[AliasCandidate] = []
    used: set[str] = set()
    # An identity contested by two equally good pairs is removed from matching
    # altogether, not merely deferred. If two candidates explain a rename equally
    # well, settling for a *worse* third explanation later would be absurd.
    contested: set[str] = set()

    for _, group in groupby(scored, key=lambda pair: pair.rank):
        live = [
            pair
            for pair in group
            if not {pair.old.fingerprint, pair.new.fingerprint} & (used | contested)
        ]
        old_counts = Counter(pair.old.fingerprint for pair in live)
        new_counts = Counter(pair.new.fingerprint for pair in live)

        for pair in live:
            if old_counts[pair.old.fingerprint] > 1 or new_counts[pair.new.fingerprint] > 1:
                contested.update({pair.old.fingerprint, pair.new.fingerprint})
                log.info(
                    "alias_ambiguous_skipped",
                    old=pair.old.display_name,
                    new=pair.new.display_name,
                    distance=round(pair.distance, 4),
                )

        for pair in live:
            if {pair.old.fingerprint, pair.new.fingerprint} & (used | contested):
                continue
            used.update({pair.old.fingerprint, pair.new.fingerprint})
            accepted.append(
                AliasCandidate(
                    old_fingerprint=pair.old.fingerprint,
                    new_fingerprint=pair.new.fingerprint,
                    distance=pair.distance,
                    certain=pair.distance <= settings.alias_certain_distance,
                )
            )

    return accepted


class _Pair:
    """A scored (disappeared, appeared) pairing."""

    __slots__ = ("distance", "new", "old", "rank")

    def __init__(self, distance: float, old: TestIdentity, new: TestIdentity) -> None:
        self.distance = distance
        self.old = old
        self.new = new
        # Ties are what trigger the ambiguity rule, so grouping must not be
        # broken by float noise from two different division orders.
        self.rank = round(distance, 9)
