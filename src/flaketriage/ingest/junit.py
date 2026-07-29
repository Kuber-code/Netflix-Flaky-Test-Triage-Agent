"""Streaming JUnit XML parser.

JUnit XML is the de-facto interchange format across ecosystems, but "JUnit XML"
is a family of dialects rather than a schema. This parser is written against the
five that matter in practice -- pytest, Maven Surefire, jest-junit,
mocha-junit-reporter and Playwright -- and is deliberately permissive about
everything else.

Two properties are non-negotiable:

1. **It streams.** Result files from a large suite run to tens of megabytes, so
   parsing uses ``iterparse`` and releases each element after use rather than
   building a full tree.
2. **It never crashes the run.** A worker killed mid-write leaves truncated XML.
   Such a file yields the cases parsed before the truncation point plus a
   :class:`ParseWarning`. Losing a whole run's results because the closing tag
   is missing is strictly worse than reporting partial data.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Final

from flaketriage.models import Frozen, Outcome, ParseWarning, TestCaseResult
from flaketriage.obs import get_logger

log = get_logger(__name__)

# Nodes that determine a case's outcome, in the order the dialects use them.
_FAILURE_TAGS: Final = {"failure": Outcome.FAIL, "error": Outcome.ERROR, "skipped": Outcome.SKIP}

# Surefire records in-run retries as siblings of the primary result. Their
# presence is same-SHA outcome divergence asserted by the runner itself.
_RERUN_TAGS: Final = frozenset({"flakyfailure", "flakyerror", "rerunfailure", "rerunerror"})

_HEAD_SCAN_BYTES: Final = 8192

# A DOCTYPE in a CI artifact has no legitimate purpose and is the vector for
# entity-expansion ("billion laughs") and external-entity attacks. Refusing the
# document outright is cheaper and more predictable than trying to configure
# expat's entity handling through ElementTree, which does not expose it.
_DOCTYPE_MARKER: Final = b"<!DOCTYPE"


class JUnitParseResult(Frozen):
    """Everything one or more result files yielded, including what went wrong."""

    cases: tuple[TestCaseResult, ...] = ()
    warnings: tuple[ParseWarning, ...] = ()
    files_parsed: int = 0
    files_rejected: int = 0

    @property
    def ok(self) -> bool:
        return self.files_rejected == 0 and not self.warnings


class _SuiteFrame:
    """One level of ``<testsuite>`` nesting.

    Reporters that model describe blocks nest suites, and the nesting is where a
    test's context lives. Cases are buffered on their frame rather than emitted
    immediately because suite-level captured output is legitimately declared
    *after* its cases -- Playwright does exactly that -- so the fallback can only
    be applied once the suite closes.
    """

    __slots__ = ("cases", "file", "name", "stderr", "stdout")

    def __init__(self, name: str, file: str | None) -> None:
        self.name = name
        self.file = file
        self.stdout: str | None = None
        self.stderr: str | None = None
        self.cases: list[TestCaseResult] = []


def parse_files(paths: Iterator[Path] | list[Path]) -> JUnitParseResult:
    """Parse several result files, accumulating cases and warnings."""
    cases: list[TestCaseResult] = []
    warnings: list[ParseWarning] = []
    parsed = 0
    rejected = 0

    for path in paths:
        result = parse_file(path)
        cases.extend(result.cases)
        warnings.extend(result.warnings)
        parsed += result.files_parsed
        rejected += result.files_rejected

    return JUnitParseResult(
        cases=tuple(cases),
        warnings=tuple(warnings),
        files_parsed=parsed,
        files_rejected=rejected,
    )


def parse_file(path: Path) -> JUnitParseResult:
    """Parse one result file. Never raises for bad input."""
    origin = str(path)
    try:
        with path.open("rb") as handle:
            rejection = _reject_unsafe_or_empty(handle, origin)
            if rejection is not None:
                log.warning("junit_file_rejected", origin=origin, reason=rejection.reason)
                return JUnitParseResult(warnings=(rejection,), files_rejected=1)
            return _parse_stream(handle, origin)
    except OSError as exc:
        warning = ParseWarning(origin=origin, reason="unreadable_file", detail=str(exc))
        log.warning("junit_file_unreadable", origin=origin, error=str(exc))
        return JUnitParseResult(warnings=(warning,), files_rejected=1)


def parse_bytes(data: bytes, origin: str = "<bytes>") -> JUnitParseResult:
    """Parse result XML already in memory. Used by tests and the eval corpus."""
    import io

    handle = io.BytesIO(data)
    rejection = _reject_unsafe_or_empty(handle, origin)
    if rejection is not None:
        return JUnitParseResult(warnings=(rejection,), files_rejected=1)
    return _parse_stream(handle, origin)


def _reject_unsafe_or_empty(handle: IO[bytes], origin: str) -> ParseWarning | None:
    """Screen a document before handing it to the XML parser."""
    head = handle.read(_HEAD_SCAN_BYTES)
    handle.seek(0)

    if not head.strip():
        return ParseWarning(origin=origin, reason="empty_file")
    if _DOCTYPE_MARKER in head.upper():
        return ParseWarning(
            origin=origin,
            reason="doctype_rejected",
            detail="DOCTYPE declarations are refused; see ingest.junit",
        )
    if b"<" not in head:
        return ParseWarning(origin=origin, reason="not_xml")
    return None


def _parse_stream(handle: IO[bytes], origin: str) -> JUnitParseResult:
    cases: list[TestCaseResult] = []
    warnings: list[ParseWarning] = []
    suites: list[_SuiteFrame] = []
    # Depth of <testcase> nesting. A <system-out> inside a case belongs to that
    # case; without this guard it would also overwrite the suite's fallback and
    # leak one test's output onto its siblings.
    case_depth = 0

    def emit(frame: _SuiteFrame) -> None:
        """Resolve a closed suite's cases and hand them to the enclosing scope."""
        resolved = [_with_suite_output(case, frame) for case in frame.cases]
        (suites[-1].cases if suites else cases).extend(resolved)

    try:
        for event, element in ET.iterparse(  # noqa: S314 -- DOCTYPE refused above
            handle, events=("start", "end")
        ):
            tag = _localname(element.tag).lower()

            if event == "start":
                if tag == "testsuite":
                    suites.append(
                        _SuiteFrame(
                            name=element.get("name", "").strip(),
                            file=_clean(element.get("file")),
                        )
                    )
                elif tag == "testcase":
                    case_depth += 1
                continue

            if tag == "testcase":
                case_depth = max(case_depth - 1, 0)
                case = _build_case(element, suites, origin)
                (suites[-1].cases if suites else cases).append(case)
                element.clear()
            elif tag == "testsuite":
                if suites:
                    emit(suites.pop())
                # Dropping the suite's children is what keeps memory flat across
                # a large file; the cases have already been converted.
                element.clear()
            elif tag in {"system-out", "system-err"} and suites and case_depth == 0:
                _attach_suite_output(suites[-1], tag, element)

    except ET.ParseError as exc:
        # Truncated or corrupt XML: keep what was recovered, record why. The
        # suite stack is still open, so it has to be drained by hand -- with a
        # truncated file, every recovered case is inside an unclosed suite.
        while suites:
            emit(suites.pop())
        warnings.append(ParseWarning(origin=origin, reason="malformed_xml", detail=str(exc)))
        log.warning(
            "junit_malformed_xml",
            origin=origin,
            error=str(exc),
            recovered_cases=len(cases),
        )
        return JUnitParseResult(
            cases=tuple(cases),
            warnings=tuple(warnings),
            files_parsed=1,
        )

    while suites:  # pragma: no cover - only reachable on unbalanced-but-valid XML
        emit(suites.pop())

    if not cases:
        warnings.append(ParseWarning(origin=origin, reason="no_testcases"))

    return JUnitParseResult(cases=tuple(cases), warnings=tuple(warnings), files_parsed=1)


def _attach_suite_output(frame: _SuiteFrame, tag: str, element: ET.Element) -> None:
    """Record suite-level captured output as a fallback for its cases.

    jest-junit and Playwright attach captured output to the suite rather than
    the case. It is weaker evidence than case-level output -- it may belong to a
    sibling test -- so it is only used when the case has none of its own.
    """
    text = _clean(element.text)
    if tag == "system-out":
        frame.stdout = text
    else:
        frame.stderr = text
    element.clear()


def _with_suite_output(case: TestCaseResult, frame: _SuiteFrame) -> TestCaseResult:
    """Fill in captured output the case did not carry itself."""
    updates: dict[str, str] = {}
    if case.stdout is None and frame.stdout is not None:
        updates["stdout"] = frame.stdout
    if case.stderr is None and frame.stderr is not None:
        updates["stderr"] = frame.stderr
    return case.model_copy(update=updates) if updates else case


def _build_case(element: ET.Element, suites: list[_SuiteFrame], origin: str) -> TestCaseResult:
    outcome = Outcome.PASS
    failure_type: str | None = None
    failure_message: str | None = None
    stack_trace: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    rerun_observed = False

    for child in element:
        child_tag = _localname(child.tag).lower()

        if child_tag in _FAILURE_TAGS and outcome is Outcome.PASS:
            # First failure-ish child in document order wins. Dialects do not
            # emit more than one, and preserving document order is the most
            # faithful reading when they do.
            outcome = _FAILURE_TAGS[child_tag]
            failure_type = _clean(child.get("type"))
            failure_message = _clean(child.get("message"))
            stack_trace = _clean(child.text)
        elif child_tag in _RERUN_TAGS:
            rerun_observed = True
            if failure_message is None:
                failure_type = _clean(child.get("type"))
                failure_message = _clean(child.get("message"))
                stack_trace = _clean(child.text)
        elif child_tag == "system-out":
            stdout = _clean(child.text)
        elif child_tag == "system-err":
            stderr = _clean(child.text)

    return TestCaseResult(
        name=element.get("name", "").strip(),
        classname=element.get("classname", element.get("class", "")).strip(),
        suite_path="/".join(frame.name for frame in suites if frame.name),
        file_path=_clean(element.get("file")) or _inherited_file(suites),
        line=_int_or_none(element.get("line")),
        duration_ms=_duration_ms(element.get("time")),
        outcome=outcome,
        failure_type=failure_type,
        failure_message=failure_message,
        stack_trace=stack_trace,
        stdout=stdout,
        stderr=stderr,
        rerun_observed=rerun_observed,
        source_file=origin,
    )


def _inherited_file(suites: list[_SuiteFrame]) -> str | None:
    """Nearest enclosing suite that declares a file.

    Nested reporters put the file on the outermost suite for a spec and leave the
    inner describe blocks bare, so only checking the innermost frame loses it.
    """
    for frame in reversed(suites):
        if frame.file:
            return frame.file
    return None


def _localname(tag: str | bytes) -> str:
    """Strip an XML namespace, which some reporters emit and most do not."""
    name = tag.decode() if isinstance(tag, bytes) else tag
    return name.rpartition("}")[2] if "}" in name else name


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _duration_ms(value: str | None) -> int | None:
    """Convert the ``time`` attribute (seconds) to integer milliseconds.

    The attribute is routinely absent, empty, localized with a comma, or
    ``NaN``. None of those is worth failing a run over.
    """
    if value is None:
        return None
    try:
        seconds = float(value.replace(",", "."))
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:  # NaN or nonsense
        return None
    return round(seconds * 1000)
