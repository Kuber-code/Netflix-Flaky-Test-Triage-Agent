"""Ingest layer: JUnit XML results, git diffs, and run metadata."""

from flaketriage.ingest.diff import (
    DiffParseResult,
    diff_from_git,
    parse_diff_file,
    parse_unified_diff,
)
from flaketriage.ingest.junit import JUnitParseResult, parse_bytes, parse_file, parse_files
from flaketriage.ingest.pipeline import expand_result_paths, ingest

__all__ = [
    "DiffParseResult",
    "JUnitParseResult",
    "diff_from_git",
    "expand_result_paths",
    "ingest",
    "parse_bytes",
    "parse_diff_file",
    "parse_file",
    "parse_files",
    "parse_unified_diff",
]
