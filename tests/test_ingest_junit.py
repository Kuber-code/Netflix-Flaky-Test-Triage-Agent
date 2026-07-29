"""JUnit parser tests, one per dialect plus the failure modes that matter."""

from __future__ import annotations

from pathlib import Path

import pytest

from flaketriage.ingest.junit import parse_bytes, parse_file, parse_files
from flaketriage.models import Outcome, TestCaseResult

FIXTURES = Path(__file__).parent / "fixtures" / "junit"


def load(name: str) -> tuple[TestCaseResult, ...]:
    result = parse_file(FIXTURES / name)
    return result.cases


def by_name(cases: tuple[TestCaseResult, ...], name: str) -> TestCaseResult:
    matches = [case for case in cases if case.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r}, got {len(matches)}"
    return matches[0]


# --- dialect coverage ------------------------------------------------------


def test_pytest_dialect() -> None:
    cases = load("pytest.xml")
    assert len(cases) == 5

    passing = by_name(cases, "test_login_succeeds")
    assert passing.outcome is Outcome.PASS
    assert passing.classname == "tests.unit.test_login"
    assert passing.file_path == "tests/unit/test_login.py"
    assert passing.line == 11
    assert passing.duration_ms == 41

    failing = by_name(cases, "test_login_retries[user=alice]")
    assert failing.outcome is Outcome.FAIL
    assert failing.failure_type == "AssertionError"
    assert failing.failure_message == "AssertionError: expected status 200, got 503"
    assert failing.stack_trace is not None
    assert "assert response.status_code == 200" in failing.stack_trace

    errored = by_name(cases, "test_order_placement")
    assert errored.outcome is Outcome.ERROR
    assert errored.stderr is not None
    assert "Retrying" in errored.stderr

    skipped = by_name(cases, "test_beta_flag")
    assert skipped.outcome is Outcome.SKIP


def test_failure_and_error_are_distinguished() -> None:
    """An assertion failure and a harness error carry different signal."""
    cases = load("pytest.xml")
    assert by_name(cases, "test_login_retries[user=alice]").outcome is Outcome.FAIL
    assert by_name(cases, "test_order_placement").outcome is Outcome.ERROR


def test_surefire_dialect_and_rerun_detection() -> None:
    cases = load("surefire.xml")
    assert len(cases) == 4

    # A <flakyFailure> means the runner retried and the retry passed: the case
    # is green overall, but the divergence is the whole point of this tool.
    flaky = by_name(cases, "chargesCardOnce")
    assert flaky.outcome is Outcome.PASS
    assert flaky.rerun_observed is True
    assert flaky.failure_type == "java.lang.AssertionError"
    assert flaky.stack_trace is not None
    assert "ThreadPoolExecutor" in flaky.stack_trace

    assert by_name(cases, "appliesDiscount").outcome is Outcome.FAIL
    assert by_name(cases, "reservesInventory").rerun_observed is False

    errored = by_name(cases, "refundsOnFailure")
    assert errored.outcome is Outcome.ERROR
    assert errored.failure_type == "java.net.ConnectException"


def test_surefire_root_testsuite_without_wrapper() -> None:
    """Surefire's root element is <testsuite>, not <testsuites>."""
    cases = load("surefire.xml")
    assert all(case.suite_path == "com.example.orders.OrderServiceTest" for case in cases)


def test_jest_dialect_failure_without_message_attribute() -> None:
    cases = load("jest-junit.xml")
    assert len(cases) == 3

    failing = by_name(cases, "cart totals settles after debounce")
    assert failing.outcome is Outcome.FAIL
    assert failing.failure_message is None
    assert failing.failure_type is None
    assert failing.stack_trace is not None
    assert "Timeout - Async callback" in failing.stack_trace
    assert failing.duration_ms == 8896


def test_playwright_dialect_falls_back_to_suite_output() -> None:
    cases = load("playwright.xml")
    assert len(cases) == 3

    failing = by_name(cases, "checkout › blocks an expired coupon")
    assert failing.outcome is Outcome.FAIL
    # Playwright attaches captured output to the suite, so the case inherits it.
    assert failing.stdout is not None
    assert "Downloading browser" in failing.stdout

    skipped = by_name(cases, "checkout › shows a receipt")
    assert skipped.outcome is Outcome.SKIP
    assert skipped.stack_trace is None


def test_nested_suites_build_a_suite_path() -> None:
    cases = load("nested-suites.xml")
    assert len(cases) == 3

    nested = by_name(cases, "handles retries")
    assert nested.suite_path == "api/orders"
    assert nested.outcome is Outcome.FAIL
    assert nested.file_path == "test/api.spec.js"  # inherited from the outer suite

    sibling = by_name(cases, "reports ready")
    assert sibling.suite_path == "api/health"


def test_namespaced_tags_are_handled() -> None:
    """Namespace prefixes must not make a document unparseable."""
    cases = load("nested-suites.xml")
    assert by_name(cases, "creates an order").outcome is Outcome.PASS


# --- failure modes ---------------------------------------------------------


def test_truncated_xml_recovers_cases_and_warns() -> None:
    """A worker killed mid-write must cost a warning, not the whole run."""
    result = parse_file(FIXTURES / "truncated.xml")

    assert [w.reason for w in result.warnings] == ["malformed_xml"]
    assert result.files_parsed == 1
    assert len(result.cases) == 2
    assert result.cases[0].name == "test_cache_warm"
    assert result.cases[1].outcome is Outcome.FAIL
    assert not result.ok


def test_empty_file_warns_without_raising() -> None:
    result = parse_file(FIXTURES / "empty.xml")
    assert [w.reason for w in result.warnings] == ["empty_file"]
    assert result.cases == ()
    assert result.files_rejected == 1


def test_non_xml_file_warns_without_raising() -> None:
    result = parse_file(FIXTURES / "not-xml.txt")
    assert [w.reason for w in result.warnings] == ["not_xml"]
    assert result.cases == ()


def test_doctype_is_refused() -> None:
    """Entity expansion is the one XML attack a CI artifact can carry."""
    result = parse_file(FIXTURES / "doctype.xml")
    assert [w.reason for w in result.warnings] == ["doctype_rejected"]
    assert result.cases == ()
    assert result.files_rejected == 1


def test_missing_file_warns_without_raising(tmp_path: Path) -> None:
    result = parse_file(tmp_path / "absent.xml")
    assert [w.reason for w in result.warnings] == ["unreadable_file"]


def test_wellformed_xml_with_no_testcases_warns() -> None:
    result = parse_bytes(b"<testsuites><testsuite name='empty'/></testsuites>")
    assert [w.reason for w in result.warnings] == ["no_testcases"]
    assert result.cases == ()


def test_parse_files_accumulates_across_dialects() -> None:
    result = parse_files(
        [
            FIXTURES / "pytest.xml",
            FIXTURES / "surefire.xml",
            FIXTURES / "truncated.xml",
            FIXTURES / "empty.xml",
        ]
    )
    assert len(result.cases) == 5 + 4 + 2
    assert result.files_parsed == 3
    assert result.files_rejected == 1
    assert {w.reason for w in result.warnings} == {"malformed_xml", "empty_file"}


# --- attribute edge cases --------------------------------------------------


@pytest.mark.parametrize(
    ("time_attr", "expected"),
    [
        ('time="1.5"', 1500),
        ('time="0"', 0),
        ('time="1,5"', 1500),  # locale-formatted, seen in the wild
        ('time=""', None),
        ('time="NaN"', None),
        ('time="-1"', None),
        ('time="fast"', None),
        ("", None),
    ],
)
def test_duration_parsing_never_raises(time_attr: str, expected: int | None) -> None:
    xml = f"<testsuite name='s'><testcase name='t' {time_attr}/></testsuite>".encode()
    (case,) = parse_bytes(xml).cases
    assert case.duration_ms == expected


def test_malformed_line_attribute_is_dropped_not_fatal() -> None:
    xml = b"<testsuite name='s'><testcase name='t' line='not-a-number'/></testsuite>"
    (case,) = parse_bytes(xml).cases
    assert case.line is None


def test_class_attribute_is_accepted_as_classname() -> None:
    xml = b"<testsuite name='s'><testcase name='t' class='pkg.Thing'/></testsuite>"
    (case,) = parse_bytes(xml).cases
    assert case.classname == "pkg.Thing"


def test_whitespace_only_message_becomes_none() -> None:
    xml = (
        b"<testsuite name='s'><testcase name='t'>"
        b"<failure message='   '>  </failure>"
        b"</testcase></testsuite>"
    )
    (case,) = parse_bytes(xml).cases
    assert case.outcome is Outcome.FAIL
    assert case.failure_message is None
    assert case.stack_trace is None


def test_first_failure_child_in_document_order_wins() -> None:
    xml = (
        b"<testsuite name='s'><testcase name='t'>"
        b"<error message='harness'/><failure message='assertion'/>"
        b"</testcase></testsuite>"
    )
    (case,) = parse_bytes(xml).cases
    assert case.outcome is Outcome.ERROR
    assert case.failure_message == "harness"


def test_case_level_output_overrides_suite_level() -> None:
    xml = (
        b"<testsuite name='s'>"
        b"<system-out>suite level</system-out>"
        b"<testcase name='a'><system-out>case level</system-out></testcase>"
        b"<testcase name='b'/>"
        b"</testsuite>"
    )
    cases = parse_bytes(xml).cases
    assert by_name(cases, "a").stdout == "case level"
    assert by_name(cases, "b").stdout == "suite level"
