"""Prompt-facing guidance for the fixed cause taxonomy (§4).

The :class:`~flaketriage.models.CauseCode` enum itself is shared vocabulary and
lives in :mod:`flaketriage.models`; what lives here is the wording shown to the
model, which is prompt material and belongs with the prompt.

The set is closed on purpose. An open-ended "what caused this?" produces prose
that no downstream system can act on and that no evaluation can score. A closed
set means the classifier's output is comparable across runs, countable in
aggregate, and gradeable against labels.

``UNKNOWN`` is a first-class member, not a failure code. See ADR-0003.
"""

from __future__ import annotations

from typing import Final

from flaketriage.models import CauseCode

# Guidance given to the model, one line per code. Written as evidence to look
# for rather than as definitions, because the model's job is to match evidence.
CAUSE_GUIDANCE: Final[dict[CauseCode, str]] = {
    CauseCode.RACE_CONDITION: (
        "Concurrency or ordering nondeterminism inside the code under test. "
        "Evidence: intermittent assertion failures on shared state; thread, "
        "executor or goroutine names in the trace; failures that depend on "
        "interleaving rather than on elapsed time."
    ),
    CauseCode.TIMING_DEPENDENCY: (
        "The test depends on wall-clock time, a sleep, or a timeout. "
        "Evidence: sleep calls, timeout exceptions, deadline exceeded, failures "
        "correlated with slow runs or loaded runners."
    ),
    CauseCode.TEST_ORDER_DEPENDENCY: (
        "The test passes alone and fails in a particular suite order. "
        "Evidence: failure confined to one shard; state that a prior test must "
        "have set; passes on rerun in isolation."
    ),
    CauseCode.EXTERNAL_DEPENDENCY: (
        "Network, third-party service, DNS, or container startup. "
        "Evidence: connection refused or reset, TLS errors, 5xx from an external "
        "host, DNS resolution failure, service not ready."
    ),
    CauseCode.SHARED_STATE_LEAK: (
        "State not reset between tests: database rows, globals, temp files, "
        "caches. Evidence: unique-constraint violations, stale reads, a value "
        "left over from another test, passes after a cleanup step."
    ),
    CauseCode.RESOURCE_EXHAUSTION: (
        "The test or its environment ran out of something. "
        "Evidence: out of memory, too many open files, port already in use, "
        "disk full, process killed for resource use."
    ),
    CauseCode.INFRA_FLAKE: (
        "The platform's own fault, not the test's: worker preemption, image pull "
        "failure, runner crash. Evidence: no test assertion failure is present "
        "at all -- the error is at the harness or scheduler level."
    ),
    CauseCode.REAL_REGRESSION: (
        "A genuine defect introduced by the change under test. "
        "Evidence: the failure is deterministic rather than intermittent, and it "
        "aligns with a diff touching code on the failing path. Choose this even "
        "though the test was reported as a flake candidate, if the evidence says "
        "so -- a real bug dismissed as noise is the costliest possible outcome."
    ),
    CauseCode.UNKNOWN: (
        "Insufficient or contradictory evidence. This is a correct and expected "
        "answer, not a failure. Use it whenever the evidence supports two causes "
        "equally well, or supports none specifically."
    ),
}


__all__ = ["CAUSE_GUIDANCE", "CauseCode"]
