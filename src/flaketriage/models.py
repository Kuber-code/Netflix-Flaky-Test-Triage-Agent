"""Shared domain vocabulary.

Kept in one module with no intra-package dependencies so that every layer can
speak the same types without the deterministic core having to import the
classifier package. See ADR-0001 for why that direction matters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Frozen(BaseModel):
    """Immutable base. Ingested facts are never mutated after parsing."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Outcome(StrEnum):
    """Outcome of one test execution.

    ``FAIL`` and ``ERROR`` are kept distinct because the distinction feeds
    classification: an assertion failure points at the code under test, while a
    harness-level error more often points at infrastructure. Collapsing them
    into a single "red" state throws away the signal.
    """

    PASS = "pass"  # noqa: S105 -- a test outcome, not a credential
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"

    @property
    def is_failure(self) -> bool:
        return self in {Outcome.FAIL, Outcome.ERROR}

    @property
    def is_pass(self) -> bool:
        return self is Outcome.PASS

    @property
    def counts_as_observation(self) -> bool:
        """Whether this outcome is evidence about stability.

        Skips are not: a test that did not run tells us nothing about whether it
        is flaky, and counting skips would dilute flake rate toward zero.
        """
        return self is not Outcome.SKIP


class ChangeType(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class RunMetadata(Frozen):
    """Identity of one CI execution of the suite.

    ``attempt`` and ``shard_id`` are load-bearing rather than decorative: the
    strongest flake signal is outcome divergence between attempts at the same
    commit SHA, and order-dependency shows up as failures confined to one shard.
    """

    commit_sha: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    branch: str | None = None
    shard_id: str | None = None
    worker_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _require_tz(self) -> Self:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return self


class TestCaseResult(Frozen):
    """One parsed ``<testcase>``, before identity resolution."""

    name: str
    classname: str = ""
    suite_path: str = ""
    file_path: str | None = None
    line: int | None = None
    duration_ms: int | None = None
    outcome: Outcome = Outcome.PASS
    failure_type: str | None = None
    failure_message: str | None = None
    stack_trace: str | None = None
    stdout: str | None = None
    stderr: str | None = None

    # Set when the report itself records a retry of this case within one run
    # (Surefire's <flakyFailure>/<rerunFailure>). This is same-SHA divergence
    # asserted by the test runner, which is the strongest signal available.
    rerun_observed: bool = False

    # File the case was parsed from, retained so warnings can be attributed.
    source_file: str | None = None


class ParseWarning(Frozen):
    """A recoverable problem encountered while parsing.

    Real CI produces truncated XML when a worker is killed mid-write. Such a
    file must degrade to "these cases, plus a warning" and never to a crash --
    losing an entire run's results because the last 40 bytes are missing is a
    worse outcome than reporting partial data.
    """

    origin: str
    reason: str
    detail: str | None = None


class LineRange(Frozen):
    """An inclusive line range on one side of a diff."""

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self

    def contains(self, line: int) -> bool:
        return self.start <= line <= self.end

    def overlaps(self, other: LineRange) -> bool:
        return self.start <= other.end and other.start <= self.end


class FileChange(Frozen):
    """Changes to one file, with the line ranges touched on each side."""

    path: str
    change_type: ChangeType = ChangeType.MODIFIED
    old_path: str | None = None
    binary: bool = False
    new_ranges: tuple[LineRange, ...] = ()
    old_ranges: tuple[LineRange, ...] = ()


class DiffSummary(Frozen):
    """The parsed diff for one run."""

    files: tuple[FileChange, ...] = ()

    def paths(self) -> frozenset[str]:
        paths = {change.path for change in self.files}
        paths.update(c.old_path for c in self.files if c.old_path is not None)
        return frozenset(paths)

    def change_for(self, path: str) -> FileChange | None:
        """Look up a change by path, tolerating differences in path shape.

        Stack traces and diffs rarely agree on path shape: a Java frame gives a
        bare ``OrderService.java``, a pytest frame a repo-relative path, and a
        diff whatever git's prefix stripping left behind. So matching is exact
        first, then by path suffix.

        A suffix match is only accepted when it is **unambiguous**. Two files
        named ``service.py`` in different packages are a normal situation, and
        guessing between them would attribute a change to the wrong test --
        precisely the kind of confident-but-wrong evidence this project treats
        as worse than no evidence.
        """
        needle = _normalize_path(path)
        suffix_matches: list[FileChange] = []

        for change in self.files:
            candidates = [change.path]
            if change.old_path is not None:
                candidates.append(change.old_path)
            normalized = [_normalize_path(candidate) for candidate in candidates]
            if needle in normalized:
                return change
            if any(candidate.endswith("/" + needle) for candidate in normalized):
                suffix_matches.append(change)

        return suffix_matches[0] if len(suffix_matches) == 1 else None

    def touches(self, path: str) -> bool:
        return self.change_for(path) is not None


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized.removeprefix("a/").removeprefix("b/")


class TestIdentity(Frozen):
    """A stable identifier for a logical test.

    ``parameters`` is split out of the name so that ``test_login[user=a]`` and
    ``test_login[user=b]`` remain distinct instances while sharing a logical
    parent -- see §6.2 and ADR-0002.
    """

    fingerprint: str = Field(min_length=1)
    suite_path: str
    test_name: str
    parameters: str = ""
    file_path: str | None = None

    @property
    def display_name(self) -> str:
        base = f"{self.suite_path}::{self.test_name}" if self.suite_path else self.test_name
        return f"{base}[{self.parameters}]" if self.parameters else base
