"""Flake-rate computation.

Two rates, because there are two genuinely different situations and collapsing
them into one number would misrepresent both.

**Divergence rate** -- the real measure. Over windows that had at least two
observations at the same commit, the fraction in which outcomes disagreed. This
is a direct measurement of non-determinism and requires nothing but counting.

**Intermittency rate** -- the fallback, used only when a pipeline never retries
and so can produce no same-commit evidence at all. It measures how often the
outcome flipped between consecutive commits. It is genuinely weaker: a test that
was fixed, or one that broke, also flips. Detections resting on it are reported
at low confidence, and the distinction is visible in the output rather than
buried in a single averaged number.

Both use an exponentially weighted moving average, so recent behaviour dominates.
A test fixed last week should not stay condemned by a bad month -- with a plain
window mean, a 50-execution window keeps a month-old outage weighted equally with
yesterday's clean runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from flaketriage.config import DetectConfig
from flaketriage.detect.history import History, WindowStatus


def ewma(values: Sequence[float], alpha: float) -> float:
    """Exponentially weighted moving average over ``values``, oldest first."""
    if not values:
        return 0.0
    smoothed = values[0]
    for value in values[1:]:
        smoothed = alpha * value + (1.0 - alpha) * smoothed
    return smoothed


def divergence_rate(history: History, config: DetectConfig | None = None) -> float:
    """EWMA of same-commit divergence over every commit that produced data.

    **The denominator is all commits, not just the retried ones.** Narrowing it to
    commits that *could* diverge looks more rigorous and is badly wrong in
    practice: real pipelines retry only on failure, so every retried commit is a
    commit that failed at least once, and a large share of them will diverge. The
    rate then reads near 100% for a test that fails one run in ten.

    Retried commits still matter -- they are what makes divergence observable at
    all -- but that is a question about whether the measurement is possible
    (:attr:`History.retry_data_available`), not about what to divide by.
    """
    settings = config or DetectConfig()
    windows = [window for window in history.windows if window.status is not WindowStatus.NO_DATA]
    if not windows:
        return 0.0
    indicators = [1.0 if window.diverged else 0.0 for window in windows]
    return ewma(indicators, settings.ewma_alpha)


def intermittency_rate(history: History, config: DetectConfig | None = None) -> float:
    """EWMA of outcome flips between consecutive commits.

    Divergent windows count as flips outright: a commit that produced both a pass
    and a failure is instability regardless of what its neighbours did.
    """
    settings = config or DetectConfig()
    statuses = [status for status in history.statuses() if status is not WindowStatus.NO_DATA]
    if len(statuses) < 2:
        return 0.0

    indicators: list[float] = []
    for previous, current in pairwise(statuses):
        if current is WindowStatus.DIVERGED or previous is WindowStatus.DIVERGED:
            indicators.append(1.0)
        else:
            indicators.append(1.0 if current is not previous else 0.0)
    return ewma(indicators, settings.ewma_alpha)


def flake_rate(history: History, config: DetectConfig | None = None) -> float:
    """The headline rate: divergence when retries exist, intermittency otherwise."""
    settings = config or DetectConfig()
    if history.retry_data_available:
        return divergence_rate(history, settings)
    return intermittency_rate(history, settings)
