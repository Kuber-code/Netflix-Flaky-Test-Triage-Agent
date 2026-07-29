"""String similarity for rename detection.

Levenshtein distance is implemented here rather than pulled in as a dependency:
it is fifteen lines, the inputs are short, and the exact normalization is part of
the contract the thresholds in ``flaketriage.toml`` are calibrated against. A
library swap that changed the normalization would silently move every alias
decision.
"""

from __future__ import annotations

from typing import Final

# Test names are short. A pathological input -- a generated name kilobytes long,
# or a suite path from a deeply nested parameterized matrix -- would make the
# O(n*m) matrix expensive for no benefit, so comparison is bounded. Truncation
# is safe here because it can only make two strings look *more* similar, and a
# false rename is guarded against separately by the uniqueness rule in alias.py.
MAX_COMPARE_LENGTH: Final = 512


def edit_distance(left: str, right: str) -> int:
    """Levenshtein distance, computed with a single rolling row."""
    first = left[:MAX_COMPARE_LENGTH]
    second = right[:MAX_COMPARE_LENGTH]

    if first == second:
        return 0
    if not first:
        return len(second)
    if not second:
        return len(first)

    # Iterate over the longer string so the row is as short as possible.
    if len(first) < len(second):
        first, second = second, first

    previous = list(range(len(second) + 1))
    for row, left_char in enumerate(first, start=1):
        current = [row]
        for column, right_char in enumerate(second, start=1):
            current.append(
                min(
                    previous[column] + 1,  # deletion
                    current[column - 1] + 1,  # insertion
                    previous[column - 1] + (left_char != right_char),  # substitution
                )
            )
        previous = current
    return previous[-1]


def normalized_distance(left: str, right: str) -> float:
    """Edit distance scaled to ``0.0`` (identical) .. ``1.0`` (nothing in common).

    Normalizing by the longer length is what makes one threshold meaningful
    across both ``test_a`` -> ``test_b`` and a sixty-character Java method name:
    a raw distance of 2 is a rewrite in the first case and a typo in the second.
    """
    if not left and not right:
        return 0.0
    distance = edit_distance(left, right)
    longest = max(len(left[:MAX_COMPARE_LENGTH]), len(right[:MAX_COMPARE_LENGTH]))
    return distance / longest if longest else 0.0
