from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flaketriage.identity.similarity import MAX_COMPARE_LENGTH, edit_distance, normalized_distance

names = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=40
)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("", "", 0),
        ("abc", "abc", 0),
        ("", "abc", 3),
        ("abc", "", 3),
        ("kitten", "sitting", 3),
        ("test_login", "test_signin", 3),
        ("flaky", "flakey", 1),
    ],
)
def test_edit_distance_known_values(left: str, right: str, expected: int) -> None:
    assert edit_distance(left, right) == expected


def test_normalized_distance_bounds() -> None:
    assert normalized_distance("", "") == 0.0
    assert normalized_distance("abc", "abc") == 0.0
    assert normalized_distance("abc", "xyz") == 1.0


def test_normalization_makes_one_threshold_meaningful() -> None:
    """Two edits is a rewrite in a short name and a typo in a long one."""
    short = normalized_distance("ab", "xy")
    long = normalized_distance("testOrderIsChargedExactlyOnce", "testOrderIsChargedExactlyOncf")
    assert short == 1.0
    assert long < 0.05


@given(names, names)
def test_distance_is_symmetric(left: str, right: str) -> None:
    assert edit_distance(left, right) == edit_distance(right, left)


@given(names)
def test_distance_to_self_is_zero(value: str) -> None:
    assert edit_distance(value, value) == 0
    assert normalized_distance(value, value) == 0.0


@given(names, names)
def test_distance_is_bounded_by_the_longer_string(left: str, right: str) -> None:
    assert edit_distance(left, right) <= max(len(left), len(right))


@given(names, names)
def test_normalized_distance_stays_in_the_unit_interval(left: str, right: str) -> None:
    assert 0.0 <= normalized_distance(left, right) <= 1.0


@given(names, names, names)
def test_triangle_inequality(left: str, middle: str, right: str) -> None:
    """Edit distance is a metric; the thresholds assume it behaves like one."""
    assert edit_distance(left, right) <= edit_distance(left, middle) + edit_distance(middle, right)


@given(names, names)
def test_distance_is_zero_only_for_equal_strings(left: str, right: str) -> None:
    assert (edit_distance(left, right) == 0) == (left == right)


def test_pathological_lengths_are_bounded_not_slow() -> None:
    """Truncation can only make strings look more similar, never less."""
    long_a = "a" * (MAX_COMPARE_LENGTH * 4)
    long_b = "a" * (MAX_COMPARE_LENGTH * 4) + "b"
    assert edit_distance(long_a, long_b) == 0
    assert normalized_distance(long_a, long_b) == 0.0
