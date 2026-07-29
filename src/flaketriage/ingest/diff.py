"""Unified-diff parser.

The classifier needs to answer "did this change touch the code this test
exercises?", which requires per-file changed line ranges rather than a file
list. ``git diff --unified=0`` gives exactly that: with zero context, each hunk
header is itself the changed range, so no line-by-line counting is needed for
the common case. Wider context is still handled, by counting ``+``/``-`` lines
within the hunk.

Parsing is tolerant by design. A malformed patch degrades to the files it could
understand plus warnings, because a diff is supporting evidence -- losing it
should weaken a classification, not fail the run.
"""

from __future__ import annotations

import re
import subprocess  # fixed argv, shell=False; see diff_from_git
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from flaketriage.models import ChangeType, DiffSummary, FileChange, LineRange, ParseWarning
from flaketriage.obs import get_logger

log = get_logger(__name__)

_DIFF_HEADER: Final = re.compile(r"^diff --git (?:a/)?(?P<old>.+?) (?:b/)?(?P<new>.+)$")
_HUNK_HEADER: Final = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_OLD_FILE: Final = re.compile(r"^--- (?P<path>.+)$")
_NEW_FILE: Final = re.compile(r"^\+\+\+ (?P<path>.+)$")
_RENAME_FROM: Final = re.compile(r"^rename from (?P<path>.+)$")
_RENAME_TO: Final = re.compile(r"^rename to (?P<path>.+)$")

_DEV_NULL: Final = "/dev/null"
_GIT_TIMEOUT_SECONDS: Final = 30


@dataclass
class _FileAccumulator:
    """Mutable scratch state for the file currently being parsed."""

    path: str | None = None
    old_path: str | None = None
    change_type: ChangeType = ChangeType.MODIFIED
    binary: bool = False
    new_ranges: list[LineRange] = field(default_factory=list)
    old_ranges: list[LineRange] = field(default_factory=list)

    def finish(self) -> FileChange | None:
        if self.path is None:
            return None
        return FileChange(
            path=self.path,
            change_type=self.change_type,
            old_path=self.old_path if self.old_path != self.path else None,
            binary=self.binary,
            new_ranges=tuple(_merge_ranges(self.new_ranges)),
            old_ranges=tuple(_merge_ranges(self.old_ranges)),
        )


class DiffParseResult(DiffSummary):
    """A parsed diff plus anything that could not be understood."""

    warnings: tuple[ParseWarning, ...] = ()


def parse_unified_diff(text: str, origin: str = "<diff>") -> DiffParseResult:
    """Parse unified-diff text into per-file changed line ranges."""
    changes: list[FileChange] = []
    warnings: list[ParseWarning] = []
    current = _FileAccumulator()
    hunk: _HunkState | None = None

    def flush() -> None:
        nonlocal current, hunk
        if hunk is not None:
            hunk.commit(current)
            hunk = None
        finished = current.finish()
        if finished is not None:
            changes.append(finished)
        current = _FileAccumulator()

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")

        if line.startswith("diff --git "):
            flush()
            header = _DIFF_HEADER.match(line)
            if header is None:
                warnings.append(
                    ParseWarning(origin=origin, reason="bad_diff_header", detail=line[:200])
                )
                continue
            current.old_path = _strip_prefix(header.group("old"))
            current.path = _strip_prefix(header.group("new"))
            continue

        if line.startswith("new file mode"):
            current.change_type = ChangeType.ADDED
            continue
        if line.startswith("deleted file mode"):
            current.change_type = ChangeType.DELETED
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.binary = True
            continue

        rename_from = _RENAME_FROM.match(line)
        if rename_from is not None:
            current.change_type = ChangeType.RENAMED
            current.old_path = _strip_prefix(rename_from.group("path"))
            continue
        rename_to = _RENAME_TO.match(line)
        if rename_to is not None:
            current.change_type = ChangeType.RENAMED
            current.path = _strip_prefix(rename_to.group("path"))
            continue

        if line.startswith("--- "):
            match = _OLD_FILE.match(line)
            if match is not None:
                path = match.group("path").strip()
                if path == _DEV_NULL:
                    current.change_type = ChangeType.ADDED
                elif current.old_path is None:
                    current.old_path = _strip_prefix(path)
            continue

        if line.startswith("+++ "):
            match = _NEW_FILE.match(line)
            if match is not None:
                path = match.group("path").strip()
                if path == _DEV_NULL:
                    current.change_type = ChangeType.DELETED
                    # A deletion has no new-side path; keep the old one so the
                    # file still appears in the summary.
                    if current.path is None:
                        current.path = current.old_path
                elif current.path is None:
                    current.path = _strip_prefix(path)
            continue

        hunk_header = _HUNK_HEADER.match(line)
        if hunk_header is not None:
            if hunk is not None:
                hunk.commit(current)
            hunk = _HunkState.from_match(hunk_header)
            continue

        if hunk is not None:
            hunk.consume(line)

    flush()

    # Only report the generic "nothing found" when nothing more specific was
    # already recorded; piling a vague warning on top of a precise one makes the
    # precise one harder to see.
    if not changes and text.strip() and not warnings:
        warnings.append(ParseWarning(origin=origin, reason="no_file_changes_found"))

    return DiffParseResult(files=tuple(changes), warnings=tuple(warnings))


class _HunkState:
    """Tracks which lines inside one hunk were actually added or removed.

    With ``--unified=0`` the header alone is sufficient, but a patch file
    supplied by hand usually has context lines, and treating those as changed
    would make the "did the diff touch this code" test far too eager.
    """

    __slots__ = ("added", "header_counts", "new_cursor", "old_cursor", "removed", "seen_body")

    def __init__(self, old_start: int, new_start: int, header_counts: tuple[int, int]) -> None:
        self.old_cursor = old_start
        self.new_cursor = new_start
        self.header_counts = header_counts
        self.added: list[int] = []
        self.removed: list[int] = []
        self.seen_body = False

    @classmethod
    def from_match(cls, match: re.Match[str]) -> _HunkState:
        return cls(
            old_start=max(int(match.group("old_start")), 1),
            new_start=max(int(match.group("new_start")), 1),
            header_counts=(
                _count_or_default(match.group("old_count")),
                _count_or_default(match.group("new_count")),
            ),
        )

    def consume(self, line: str) -> None:
        if line.startswith("+") and not line.startswith("+++"):
            self.seen_body = True
            self.added.append(self.new_cursor)
            self.new_cursor += 1
        elif line.startswith("-") and not line.startswith("---"):
            self.seen_body = True
            self.removed.append(self.old_cursor)
            self.old_cursor += 1
        elif line.startswith((" ", "\t")) or not line:
            self.new_cursor += 1
            self.old_cursor += 1
        elif line.startswith("\\"):  # "\ No newline at end of file"
            pass

    def commit(self, target: _FileAccumulator) -> None:
        if self.seen_body:
            target.new_ranges.extend(_ranges_from_lines(self.added))
            target.old_ranges.extend(_ranges_from_lines(self.removed))
            return

        # No body lines were seen -- the hunk header is all we have, which is the
        # normal case for a summary-only or --unified=0 patch fragment.
        old_count, new_count = self.header_counts
        if new_count > 0:
            start = self.new_cursor
            target.new_ranges.append(LineRange(start=start, end=start + new_count - 1))
        if old_count > 0:
            start = self.old_cursor
            target.old_ranges.append(LineRange(start=start, end=start + old_count - 1))


def _count_or_default(value: str | None) -> int:
    """An omitted count in a hunk header means exactly one line."""
    return 1 if value is None else int(value)


def _ranges_from_lines(lines: list[int]) -> list[LineRange]:
    ranges: list[LineRange] = []
    for line in sorted(lines):
        if ranges and line == ranges[-1].end + 1:
            ranges[-1] = LineRange(start=ranges[-1].start, end=line)
        else:
            ranges.append(LineRange(start=line, end=line))
    return ranges


def _merge_ranges(ranges: list[LineRange]) -> list[LineRange]:
    merged: list[LineRange] = []
    for current in sorted(ranges, key=lambda r: (r.start, r.end)):
        if merged and current.start <= merged[-1].end + 1:
            merged[-1] = LineRange(start=merged[-1].start, end=max(merged[-1].end, current.end))
        else:
            merged.append(current)
    return merged


def _strip_prefix(path: str) -> str:
    cleaned = path.strip().strip('"')
    for prefix in ("a/", "b/", "./"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def parse_diff_file(path: Path) -> DiffParseResult:
    """Read and parse a patch file. Unreadable files degrade to a warning."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("diff_file_unreadable", origin=str(path), error=str(exc))
        return DiffParseResult(
            warnings=(ParseWarning(origin=str(path), reason="unreadable_file", detail=str(exc)),)
        )
    return parse_unified_diff(text, origin=str(path))


def diff_from_git(base: str, head: str = "HEAD", *, cwd: Path | None = None) -> DiffParseResult:
    """Run ``git diff --unified=0`` and parse the result.

    The argv is fixed and no shell is involved; only the two revision arguments
    come from the caller, and git treats them as revisions rather than as
    options because they follow the flags.
    """
    argv = ["git", "diff", "--unified=0", "--no-color", "--find-renames", f"{base}..{head}"]
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, shell=False
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git_diff_failed", error=str(exc))
        return DiffParseResult(
            warnings=(ParseWarning(origin="git", reason="git_unavailable", detail=str(exc)),)
        )

    if completed.returncode != 0:
        log.warning("git_diff_nonzero", returncode=completed.returncode)
        return DiffParseResult(
            warnings=(
                ParseWarning(
                    origin="git",
                    reason="git_diff_failed",
                    detail=completed.stderr.strip()[:500],
                ),
            )
        )

    return parse_unified_diff(completed.stdout, origin=f"git:{base}..{head}")
