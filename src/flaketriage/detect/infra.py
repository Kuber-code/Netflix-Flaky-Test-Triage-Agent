"""Deterministic identification of platform-level failures.

``INFRA_FLAKE`` must never count toward a test's flake rate. Attributing a
runner preemption or an image-pull failure to the test's author poisons the
metric and destroys trust in the tool -- once an engineer sees their test blamed
for a platform outage, every number this tool produces becomes suspect.

The exclusion is implemented here, deterministically, rather than waiting for the
classifier: the flake rate is computed before any model runs, and a metric that
is only correct when the LLM layer is enabled would not be a deterministic core.

**The error asymmetry matters.** A false positive marks a genuine test failure as
infrastructure and hides a real flake. A false negative merely leaves a platform
failure in the denominator, which is visible and self-correcting. So matching is
conservative: only unambiguously platform-level phrases, configured in
``flaketriage.toml`` rather than hardcoded.
"""

from __future__ import annotations

from flaketriage.config import DetectConfig
from flaketriage.models import Outcome
from flaketriage.store.repositories import ExecutionRecord


def is_infra_failure(record: ExecutionRecord, config: DetectConfig | None = None) -> bool:
    """Whether an execution failed because of the platform, not the test.

    An assertion failure is never infrastructure, whatever its message says: if
    the test framework got far enough to evaluate an assertion, the harness was
    working. Only ``error`` outcomes -- harness-level failures -- are candidates.
    """
    if record.outcome is not Outcome.ERROR:
        return False

    settings = config or DetectConfig()
    haystack = " ".join(
        part.lower()
        for part in (record.failure_type, record.failure_message, record.stack_trace)
        if part
    )
    if not haystack:
        return False
    return any(pattern.lower() in haystack for pattern in settings.infra_error_patterns)
