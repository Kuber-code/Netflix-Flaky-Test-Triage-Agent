from __future__ import annotations

import pytest

from flaketriage.identity.fingerprint import (
    fingerprint,
    identity_for,
    normalize_suite_path,
    split_parameters,
)
from flaketriage.models import TestCaseResult


@pytest.mark.parametrize(
    ("raw", "base", "params"),
    [
        ("test_login", "test_login", ""),
        ("test_login[user=alice]", "test_login", "user=alice"),
        ("test_login[user=alice-role=admin]", "test_login", "user=alice-role=admin"),
        ("test_matrix[a[0]=1]", "test_matrix", "a[0]=1"),  # nested brackets
        ("testAdd(int, int)[1]", "testAdd(int, int)", "1"),  # JUnit 5 display name
        ("test_login[]", "test_login", ""),
        ("  test_login[x]  ", "test_login", "x"),
        ("[tagged]", "[tagged]", ""),  # a name, not a parameter list
        ("test_arr[0]_suffix", "test_arr[0]_suffix", ""),  # bracket not at the end
    ],
)
def test_split_parameters(raw: str, base: str, params: str) -> None:
    assert split_parameters(raw) == (base, params)


def test_parameterized_instances_share_a_base_but_differ() -> None:
    """Distinct instances, one logical parent -- both properties are required."""
    alice = identity_for(_case("test_login[user=alice]", file_path="tests/test_login.py"))
    bob = identity_for(_case("test_login[user=bob]", file_path="tests/test_login.py"))

    assert alice.test_name == bob.test_name == "test_login"
    assert alice.suite_path == bob.suite_path
    assert alice.parameters != bob.parameters
    assert alice.fingerprint != bob.fingerprint


@pytest.mark.parametrize(
    ("classname", "suite_path", "file_path", "expected"),
    [
        # pytest: dotted module path, plus a real file path when available.
        ("tests.unit.test_login", "pytest", "tests/unit/test_login.py", "tests/unit/test_login.py"),
        # No file: the dotted module becomes a path.
        ("tests.unit.test_login", "pytest", None, "tests/unit/test_login"),
        # Java FQCN: the class stays distinct from its package.
        ("com.example.orders.OrderTest", "", None, "com/example/orders::OrderTest"),
        # File plus class: the class is the only part that adds information.
        ("com.example.orders.OrderTest", "", "src/OrderTest.java", "src/OrderTest.java::OrderTest"),
        # jest-junit sometimes puts a path in classname.
        ("src/checkout/cart.test.ts", "", None, "src/checkout/cart.test.ts"),
        # Nested reporters: only the suite stack is available.
        ("", "api/orders", None, "api/orders"),
        # Windows separators and ./ noise normalize away.
        ("", "", ".\\tests\\unit\\test_a.py", "tests/unit/test_a.py"),
    ],
)
def test_normalize_suite_path_across_dialects(
    classname: str, suite_path: str, file_path: str | None, expected: str
) -> None:
    assert (
        normalize_suite_path(classname=classname, suite_path=suite_path, file_path=file_path)
        == expected
    )


def test_fingerprint_is_stable_and_fixed_width() -> None:
    first = fingerprint("tests/test_a.py", "test_x", "p=1")
    second = fingerprint("tests/test_a.py", "test_x", "p=1")
    assert first == second
    assert len(first) == 16


def test_fingerprint_separates_fields_unambiguously() -> None:
    """Concatenation without a separator would collide on shifted boundaries."""
    assert fingerprint("a/b", "c") != fingerprint("a", "b/c")
    assert fingerprint("a", "b", "c") != fingerprint("a", "bc", "")


def test_path_shape_does_not_change_identity() -> None:
    """A reporter switching from ./x to x must not orphan a test's history."""
    plain = identity_for(_case("test_x", file_path="tests/test_a.py"))
    noisy = identity_for(_case("test_x", file_path=".\\tests\\test_a.py"))
    assert plain.fingerprint == noisy.fingerprint


def test_display_name_round_trips_parameters() -> None:
    identity = identity_for(_case("test_login[user=alice]", file_path="tests/test_login.py"))
    assert identity.display_name == "tests/test_login.py::test_login[user=alice]"


def test_identity_survives_a_reporter_without_a_file_path() -> None:
    """Same test, two reporters: this is the case aliasing exists to handle."""
    with_file = identity_for(
        _case("test_x", classname="tests.unit.test_a", file_path="tests/unit/test_a.py")
    )
    without_file = identity_for(_case("test_x", classname="tests.unit.test_a"))
    # Documented limitation, not an accident: the fingerprints differ, which is
    # what alias resolution (P2) exists to reconcile.
    assert with_file.fingerprint != without_file.fingerprint
    assert with_file.test_name == without_file.test_name


def _case(
    name: str, *, classname: str = "", suite_path: str = "", file_path: str | None = None
) -> TestCaseResult:
    return TestCaseResult(
        name=name, classname=classname, suite_path=suite_path, file_path=file_path
    )
