from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flaketriage.ingest.pipeline import expand_result_paths, ingest
from flaketriage.models import Outcome, RunMetadata
from flaketriage.store.db import IN_MEMORY
from flaketriage.store.repositories import RunStore

FIXTURES = Path(__file__).parent / "fixtures" / "junit"


@pytest.fixture
def store() -> Iterator[RunStore]:
    with RunStore.open(IN_MEMORY) as opened:
        yield opened


def metadata(**overrides: object) -> RunMetadata:
    defaults: dict[str, object] = {
        "commit_sha": "deadbeefcafe",
        "run_id": "run-1",
        "started_at": datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return RunMetadata.model_validate(defaults)


def test_end_to_end_ingest_persists_identities_and_executions(store: RunStore) -> None:
    summary = ingest(store, metadata(), [FIXTURES / "pytest.xml"])

    assert summary.cases_ingested == 5
    assert summary.new_identities == 5
    assert summary.cases_skipped_duplicate == 0
    assert summary.warnings == ()
    assert store.count_executions() == 5

    records = store.executions_for_run(summary.run_pk)
    outcomes = sorted(record.outcome.value for record in records)
    assert outcomes == ["error", "fail", "pass", "pass", "skip"]


def test_parameterized_cases_become_separate_identities(store: RunStore) -> None:
    ingest(store, metadata(), [FIXTURES / "pytest.xml"])
    rows = store.connection.execute(
        """
        SELECT parameters FROM test_identities
         WHERE test_name = 'test_login_retries' ORDER BY parameters
        """
    ).fetchall()
    assert [row["parameters"] for row in rows] == ["user=alice", "user=bob"]


def test_reingest_of_the_same_run_is_a_no_op(store: RunStore) -> None:
    """Observation counts feed flake rate, so double counting corrupts everything."""
    first = ingest(store, metadata(), [FIXTURES / "pytest.xml"])
    second = ingest(store, metadata(), [FIXTURES / "pytest.xml"])

    assert second.run_pk == first.run_pk
    assert second.cases_ingested == 0
    assert second.cases_skipped_duplicate == 5
    assert second.new_identities == 0
    assert store.count_executions() == 5


def test_a_retry_attempt_adds_observations_to_the_same_identities(store: RunStore) -> None:
    """This is the shape the strongest flake signal is read from."""
    ingest(store, metadata(attempt=1), [FIXTURES / "pytest.xml"])
    ingest(store, metadata(attempt=2), [FIXTURES / "pytest.xml"])

    assert store.count_identities() == 5
    assert store.count_executions() == 10


def test_truncated_file_yields_partial_data_and_a_persisted_warning(store: RunStore) -> None:
    summary = ingest(store, metadata(), [FIXTURES / "truncated.xml"])

    assert summary.cases_ingested == 2
    assert [w.reason for w in summary.warnings] == ["malformed_xml"]
    assert [w.reason for w in store.warnings_for_run(summary.run_pk)] == ["malformed_xml"]


def test_a_run_of_only_broken_files_still_records_the_run(store: RunStore) -> None:
    """Losing the record of a failed ingest hides the problem entirely."""
    summary = ingest(store, metadata(), [FIXTURES / "empty.xml", FIXTURES / "doctype.xml"])

    assert summary.cases_ingested == 0
    assert {w.reason for w in summary.warnings} == {"empty_file", "doctype_rejected"}
    assert len(store.warnings_for_run(summary.run_pk)) == 2


def test_multiple_dialects_in_one_run(store: RunStore) -> None:
    """Sharded polyglot pipelines write several reporters' output per run."""
    summary = ingest(
        store,
        metadata(),
        [
            FIXTURES / "pytest.xml",
            FIXTURES / "surefire.xml",
            FIXTURES / "jest-junit.xml",
            FIXTURES / "playwright.xml",
            FIXTURES / "nested-suites.xml",
        ],
    )
    assert summary.cases_ingested == 5 + 4 + 3 + 3 + 3
    assert summary.new_identities == summary.cases_ingested


def test_rerun_flag_survives_ingest(store: RunStore) -> None:
    summary = ingest(store, metadata(), [FIXTURES / "surefire.xml"])
    records = store.executions_for_run(summary.run_pk)
    reruns = [record for record in records if record.rerun_observed]
    assert len(reruns) == 1
    assert reruns[0].outcome is Outcome.PASS


def test_diff_is_recorded_against_the_run(store: RunStore, tmp_path: Path) -> None:
    from flaketriage.ingest.diff import parse_unified_diff

    diff = parse_unified_diff(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    summary = ingest(store, metadata(), [FIXTURES / "pytest.xml"], diff=diff)
    assert summary.diff_files == 1
    assert store.diff_paths_for_sha("deadbeefcafe") == frozenset({"src/a.py"})


# --- path expansion -------------------------------------------------------


def test_expand_explicit_files() -> None:
    paths = expand_result_paths([FIXTURES / "pytest.xml", FIXTURES / "surefire.xml"])
    assert [path.name for path in paths] == ["pytest.xml", "surefire.xml"]


def test_expand_a_directory_recursively(tmp_path: Path) -> None:
    (tmp_path / "shard-1").mkdir()
    (tmp_path / "shard-1" / "results.xml").write_text("<testsuite/>", encoding="utf-8")
    (tmp_path / "top.xml").write_text("<testsuite/>", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    names = [path.name for path in expand_result_paths([tmp_path])]
    assert names == ["results.xml", "top.xml"]


def test_expand_a_glob_because_windows_shells_do_not(tmp_path: Path) -> None:
    """PowerShell and cmd pass globs through verbatim."""
    for name in ("a.xml", "b.xml", "c.txt"):
        (tmp_path / name).write_text("<testsuite/>", encoding="utf-8")

    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        names = [path.name for path in expand_result_paths(["*.xml"])]
    finally:
        os.chdir(previous)
    assert names == ["a.xml", "b.xml"]


def test_expand_deduplicates_overlapping_patterns(tmp_path: Path) -> None:
    target = tmp_path / "results.xml"
    target.write_text("<testsuite/>", encoding="utf-8")
    assert expand_result_paths([tmp_path, target]) == [target]


def test_expand_unmatched_pattern_returns_nothing(tmp_path: Path) -> None:
    assert expand_result_paths([tmp_path / "nope-*.xml"]) == []
