"""Metric computation for the evaluation harness.

Per-class precision and recall rather than overall accuracy, because the classes
are imbalanced and an overall figure hides exactly the behaviour that matters: a
classifier that never predicts ``REAL_REGRESSION`` can still post a respectable
accuracy number.

The **dangerous-error rate** is separated out and reported first. It counts
``REAL_REGRESSION`` examples classified as any flake category -- the error that
tells an engineer to ignore a real bug. It is the only metric here with an
asymmetric cost, so it gets its own line rather than being averaged into the rest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from flaketriage.models import CauseCode

FLAKE_CATEGORIES = frozenset(code for code in CauseCode if code.is_flake_category)


@dataclass
class ClassMetrics:
    label: CauseCode
    support: int = 0
    predicted: int = 0
    true_positive: int = 0

    @property
    def precision(self) -> float:
        return self.true_positive / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        denominator = self.precision + self.recall
        if denominator == 0.0:
            return 0.0
        return 2 * self.precision * self.recall / denominator


@dataclass
class Report:
    """Everything the results table needs for one classifier."""

    name: str
    total: int = 0
    correct: int = 0
    abstentions: int = 0
    dangerous_errors: int = 0
    regression_support: int = 0
    adversarial_total: int = 0
    adversarial_correct: int = 0
    per_class: dict[CauseCode, ClassMetrics] = field(default_factory=dict)
    confusion: dict[tuple[CauseCode, CauseCode], int] = field(default_factory=dict)
    downgrade_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def abstention_rate(self) -> float:
        return self.abstentions / self.total if self.total else 0.0

    @property
    def answered(self) -> int:
        return self.total - self.abstentions

    @property
    def accuracy_when_answering(self) -> float:
        """Accuracy over non-abstained predictions.

        Reported alongside the abstention rate, never instead of it: either number
        alone can be improved by moving the confidence floor in one direction.
        """
        if not self.answered:
            return 0.0
        answered_correct = sum(
            count
            for (truth, predicted), count in self.confusion.items()
            if truth == predicted and predicted is not CauseCode.UNKNOWN
        )
        return answered_correct / self.answered

    @property
    def dangerous_error_rate(self) -> float:
        """Rate of REAL_REGRESSION examples classified as a flake."""
        if not self.regression_support:
            return 0.0
        return self.dangerous_errors / self.regression_support

    @property
    def adversarial_accuracy(self) -> float:
        if not self.adversarial_total:
            return 0.0
        return self.adversarial_correct / self.adversarial_total

    @property
    def macro_f1(self) -> float:
        """Unweighted mean F1 over classes with support.

        Unweighted on purpose: it refuses to let good performance on the largest
        class paper over a class the classifier never gets right.
        """
        scored = [metrics.f1 for metrics in self.per_class.values() if metrics.support]
        return sum(scored) / len(scored) if scored else 0.0


def score(
    name: str,
    labels: Sequence[CauseCode],
    predictions: Sequence[CauseCode],
    *,
    adversarial: Sequence[bool] | None = None,
    downgrade_reasons: Sequence[str] | None = None,
) -> Report:
    """Compute a full report from parallel label/prediction sequences."""
    if len(labels) != len(predictions):
        raise ValueError("labels and predictions must be the same length")

    report = Report(name=name, total=len(labels))
    flags = list(adversarial or [False] * len(labels))
    reasons = list(downgrade_reasons or [""] * len(labels))

    for code in CauseCode:
        report.per_class[code] = ClassMetrics(label=code)

    for index, (truth, predicted) in enumerate(zip(labels, predictions, strict=True)):
        report.per_class[truth].support += 1
        report.per_class[predicted].predicted += 1
        report.confusion[(truth, predicted)] = report.confusion.get((truth, predicted), 0) + 1

        if truth is predicted:
            report.correct += 1
            report.per_class[truth].true_positive += 1

        if predicted is CauseCode.UNKNOWN:
            report.abstentions += 1

        if truth is CauseCode.REAL_REGRESSION:
            report.regression_support += 1
            if predicted in FLAKE_CATEGORIES:
                report.dangerous_errors += 1

        if flags[index]:
            report.adversarial_total += 1
            if truth is predicted:
                report.adversarial_correct += 1

        reason = reasons[index]
        if reason and reason != "none":
            report.downgrade_reasons[reason] = report.downgrade_reasons.get(reason, 0) + 1

    return report


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. Small samples make interpolation false precision."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(round(fraction * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]
