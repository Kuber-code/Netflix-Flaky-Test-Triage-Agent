"""Keyword-heuristic baseline classifier.

This is the answer to "why not just use regex on stack traces?" -- implemented in
good faith rather than as a straw man, because a baseline built to lose proves
nothing. The rules below are the ones a competent engineer would write in an
afternoon after looking at a few hundred failures: match the unambiguous
signatures, order the rules so the specific ones win, and abstain otherwise.

It has no access to anything a regex cannot see. It cannot tell a deterministic
failure from an intermittent one, cannot read a diff, and cannot notice that a
trace's mechanism contradicts its message. Where those things do not matter it
should do well, and where they do it should not -- and reporting both halves is
the point.
"""

from __future__ import annotations

import re
from typing import Final

from flaketriage.models import CauseCode

#: Ordered (cause, pattern) rules. First match wins, so the most specific
#: signatures come first: "no space left on device" is infrastructure even though
#: it would also match the resource-exhaustion rules below it.
RULES: Final[tuple[tuple[CauseCode, re.Pattern[str]], ...]] = (
    # Platform-level first: these must never be attributed to the test.
    (
        CauseCode.INFRA_FLAKE,
        re.compile(
            r"no space left on device|imagepullbackoff|error pulling image"
            r"|manifest unknown|shutdown signal|spot instance|received sigkill"
            r"|exited with code 137|lost communication|preempted|agentlost",
            re.IGNORECASE,
        ),
    ),
    (
        CauseCode.RESOURCE_EXHAUSTION,
        re.compile(
            r"outofmemoryerror|out of memory|heap space|heap limit"
            r"|too many open files|address already in use|errno 24|errno 98"
            r"|cannot allocate memory",
            re.IGNORECASE,
        ),
    ),
    (
        CauseCode.EXTERNAL_DEPENDENCY,
        re.compile(
            r"connectionreset|connection refused|connection reset|unknownhost"
            r"|name or service not known|sslerror|tlsv1|status code 5\d\d"
            r"|service unavailable|econnrefused|dial tcp|i/o timeout"
            r"|did not become healthy|containernotready",
            re.IGNORECASE,
        ),
    ),
    (
        CauseCode.SHARED_STATE_LEAK,
        re.compile(
            r"uniqueviolation|duplicate key|unique constraint|fileexistserror"
            r"|already exists|stale|never reset|module-level",
            re.IGNORECASE,
        ),
    ),
    (
        CauseCode.RACE_CONDITION,
        re.compile(
            r"data race|threadpoolexecutor|goroutine|concurrent\.futures"
            r"|race condition|interleav|same instance",
            re.IGNORECASE,
        ),
    ),
    (
        CauseCode.TIMING_DEPENDENCY,
        re.compile(
            r"timeout|timed out|deadline exceeded|time\.sleep|jest\.settimeout"
            r"|waiting for|date\.today|monotonic",
            re.IGNORECASE,
        ),
    ),
    (
        CauseCode.TEST_ORDER_DEPENDENCY,
        re.compile(
            r"passes when run with|only fails when scheduled|still installed"
            r"|set by a prior test|keyerror",
            re.IGNORECASE,
        ),
    ),
)


def classify(
    *,
    failure_type: str | None,
    failure_message: str | None,
    stack_trace: str | None,
    failing_shards: tuple[str, ...] = (),
) -> CauseCode:
    """Best keyword guess, or ``UNKNOWN`` when nothing matches."""
    haystack = "\n".join(part for part in (failure_type, failure_message, stack_trace) if part)
    if not haystack.strip():
        return CauseCode.UNKNOWN

    for cause, pattern in RULES:
        if pattern.search(haystack):
            return cause

    # A failure confined to one shard is the one non-textual signal cheap enough
    # for a heuristic to use, and it is the classic order-dependency tell.
    if len(failing_shards) == 1:
        return CauseCode.TEST_ORDER_DEPENDENCY

    return CauseCode.UNKNOWN
