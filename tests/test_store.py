from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flaketriage.models import (
    ChangeType,
    DiffSummary,
    FileChange,
    LineRange,
    Outcome,
    ParseWarning,
    RunMetadata,
    TestCaseResult,
    TestIdentity,
)
from flaketriage.store.db import IN_MEMORY, SchemaTooNewError, apply_migrations, connect
from flaketriage.store.repositories import RunStore
from flaketriage.store.schema import SCHEMA_VERSION


@pytest.fixture
def store() -> Iterator[RunStore]:
    with RunStore.open(IN_MEMORY) as opened:
        yield opened


def metadata(**overrides: object) -> RunMetadata:
    defaults: dict[str, object] = {
        "commit_sha": "abc123def456",
        "run_id": "run-1",
        "attempt": 1,
        "branch": "main",
        "started_at": datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return RunMetadata.model_validate(defaults)


def identity(name: str = "test_x", params: str = "") -> TestIdentity:
    return TestIdentity(
        fingerprint=f"fp-{name}-{params}",
        suite_path="tests/test_a.py",
        test_name=name,
        parameters=params,
        file_path="tests/test_a.py",
    )


def case(outcome: Outcome = Outcome.FAIL, **overrides: object) -> TestCaseResult:
    defaults: dict[str, object] = {"name": "test_x", "outcome": outcome}
    defaults.update(overrides)
    return TestCaseResult.model_validate(defaults)


# --- schema ---------------------------------------------------------------


def test_migrations_are_applied_on_connect() -> None:
    connection = connect(IN_MEMORY)
    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "runs",
        "test_identities",
        "identity_aliases",
        "executions",
        "diff_files",
        "diff_hunks",
        "parse_warnings",
    } <= tables


def test_migrations_are_idempotent() -> None:
    connection = connect(IN_MEMORY)
    assert apply_migrations(connection) == SCHEMA_VERSION
    assert apply_migrations(connection) == SCHEMA_VERSION


def test_a_newer_schema_is_refused_rather_than_misread() -> None:
    connection = connect(IN_MEMORY)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(SchemaTooNewError):
        apply_migrations(connection)


def test_store_file_is_created_with_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "store.sqlite"
    connect(path).close()
    assert path.is_file()


def test_foreign_keys_are_enforced() -> None:
    """Without PRAGMA foreign_keys the ON DELETE CASCADE clauses are decorative."""
    connection = connect(IN_MEMORY)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO executions (run_pk, identity_id, outcome, created_at)
            VALUES (999, 999, 'fail', '2026-07-20T10:00:00Z')
            """
        )


def test_outcome_check_constraint_rejects_unknown_values(store: RunStore) -> None:
    run_pk = store.record_run(metadata())
    identity_id, _ = store.upsert_identity(identity())
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            """
            INSERT INTO executions (run_pk, identity_id, outcome, created_at)
            VALUES (?, ?, 'exploded', '2026-07-20T10:00:00Z')
            """,
            (run_pk, identity_id),
        )


def test_deleting_a_run_cascades_to_its_executions(store: RunStore) -> None:
    run_pk = store.record_run(metadata())
    identity_id, _ = store.upsert_identity(identity())
    store.record_executions(run_pk, [(identity_id, case())])
    assert store.count_executions() == 1

    store.connection.execute("DELETE FROM runs WHERE id = ?", (run_pk,))
    assert store.count_executions() == 0


# --- idempotency ----------------------------------------------------------


def test_reingesting_a_run_returns_the_same_key(store: RunStore) -> None:
    """CI retries ingest steps; a second run row would double every count."""
    first = store.record_run(metadata())
    second = store.record_run(metadata())
    assert first == second
    assert store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_attempts_and_shards_are_distinct_runs(store: RunStore) -> None:
    base = store.record_run(metadata())
    retry = store.record_run(metadata(attempt=2))
    sharded = store.record_run(metadata(shard_id="3"))
    assert len({base, retry, sharded}) == 3


def test_absent_shard_normalizes_so_uniqueness_holds(store: RunStore) -> None:
    """SQLite treats NULLs as distinct, so optional keys default to ''."""
    first = store.record_run(metadata(shard_id=None))
    second = store.record_run(metadata(shard_id=None))
    assert first == second


def test_duplicate_executions_are_skipped_not_doubled(store: RunStore) -> None:
    run_pk = store.record_run(metadata())
    identity_id, _ = store.upsert_identity(identity())

    inserted, skipped = store.record_executions(run_pk, [(identity_id, case())])
    assert (inserted, skipped) == (1, 0)

    inserted, skipped = store.record_executions(run_pk, [(identity_id, case())])
    assert (inserted, skipped) == (0, 1)
    assert store.count_executions() == 1


def test_upsert_identity_reports_creation_once(store: RunStore) -> None:
    first_id, created = store.upsert_identity(identity())
    assert created is True
    second_id, created = store.upsert_identity(identity())
    assert created is False
    assert first_id == second_id
    assert store.count_identities() == 1


def test_upsert_identity_backfills_a_missing_file_path(store: RunStore) -> None:
    """A reporter that starts emitting file paths must enrich, not erase."""
    bare = identity().model_copy(update={"file_path": None})
    identity_id, _ = store.upsert_identity(bare)
    stored = store.identity_by_id(identity_id)
    assert stored is not None
    assert stored.file_path is None

    store.upsert_identity(identity())
    enriched = store.identity_by_id(identity_id)
    assert enriched is not None
    assert enriched.file_path == "tests/test_a.py"

    store.upsert_identity(bare)
    still_there = store.identity_by_id(identity_id)
    assert still_there is not None
    assert still_there.file_path == "tests/test_a.py"


# --- reads ----------------------------------------------------------------


def test_executions_for_identity_returns_newest_first(store: RunStore) -> None:
    identity_id, _ = store.upsert_identity(identity())
    for index, outcome in enumerate([Outcome.PASS, Outcome.FAIL, Outcome.PASS]):
        run_pk = store.record_run(
            metadata(
                run_id=f"run-{index}",
                started_at=datetime(2026, 7, 20, 10, index, tzinfo=UTC),
            )
        )
        store.record_executions(run_pk, [(identity_id, case(outcome))])

    records = store.executions_for_identity(identity_id)
    assert [record.run_id for record in records] == ["run-2", "run-1", "run-0"]
    assert [record.outcome for record in records] == [Outcome.PASS, Outcome.FAIL, Outcome.PASS]


def test_executions_for_identity_respects_the_window(store: RunStore) -> None:
    identity_id, _ = store.upsert_identity(identity())
    for index in range(5):
        run_pk = store.record_run(
            metadata(
                run_id=f"run-{index}",
                started_at=datetime(2026, 7, 20, 10, index, tzinfo=UTC),
            )
        )
        store.record_executions(run_pk, [(identity_id, case())])

    assert len(store.executions_for_identity(identity_id, limit=3)) == 3


def test_execution_records_carry_their_run_context(store: RunStore) -> None:
    """The detector needs sha, attempt and shard alongside every outcome."""
    run_pk = store.record_run(metadata(attempt=2, shard_id="7"))
    identity_id, _ = store.upsert_identity(identity())
    store.record_executions(
        run_pk,
        [(identity_id, case(failure_message="boom", stack_trace="frame", rerun_observed=True))],
    )

    (record,) = store.executions_for_run(run_pk)
    assert record.commit_sha == "abc123def456"
    assert record.attempt == 2
    assert record.shard_id == "7"
    assert record.branch == "main"
    assert record.failure_message == "boom"
    assert record.rerun_observed is True


def test_failing_identities_at_sha_covers_fail_and_error_only(store: RunStore) -> None:
    run_pk = store.record_run(metadata())
    ids = {}
    for name, outcome in [
        ("failed", Outcome.FAIL),
        ("errored", Outcome.ERROR),
        ("passed", Outcome.PASS),
        ("skipped", Outcome.SKIP),
    ]:
        identity_id, _ = store.upsert_identity(identity(name))
        ids[name] = identity_id
        store.record_executions(run_pk, [(identity_id, case(outcome))])

    failing = set(store.failing_identities_at_sha("abc123def456"))
    assert failing == {ids["failed"], ids["errored"]}


def test_identity_lookup_by_fingerprint(store: RunStore) -> None:
    store.upsert_identity(identity("test_y", "p=1"))
    found = store.identity_by_fingerprint("fp-test_y-p=1")
    assert found is not None
    assert found.test_name == "test_y"
    assert found.parameters == "p=1"
    assert store.identity_by_fingerprint("absent") is None
    assert store.identity_id_by_fingerprint("absent") is None
    assert store.identity_by_id(9999) is None


# --- diffs and warnings ---------------------------------------------------


def test_diff_is_persisted_with_hunks(store: RunStore) -> None:
    run_pk = store.record_run(metadata())
    diff = DiffSummary(
        files=(
            FileChange(
                path="src/orders/service.py",
                change_type=ChangeType.MODIFIED,
                new_ranges=(LineRange(start=42, end=44),),
                old_ranges=(LineRange(start=88, end=88),),
            ),
            FileChange(path="assets/logo.png", change_type=ChangeType.MODIFIED, binary=True),
        )
    )
    assert store.record_diff(run_pk, diff) == 2
    assert store.diff_paths_for_sha("abc123def456") == frozenset(
        {"src/orders/service.py", "assets/logo.png"}
    )

    hunks = store.connection.execute(
        """
        SELECT h.side, h.start_line, h.end_line
          FROM diff_hunks h JOIN diff_files f ON f.id = h.diff_file_id
         WHERE f.path = 'src/orders/service.py'
         ORDER BY h.side
        """
    ).fetchall()
    assert [(row["side"], row["start_line"], row["end_line"]) for row in hunks] == [
        ("new", 42, 44),
        ("old", 88, 88),
    ]


def test_recording_the_same_diff_twice_does_not_duplicate_hunks(store: RunStore) -> None:
    run_pk = store.record_run(metadata())
    diff = DiffSummary(files=(FileChange(path="a.py", new_ranges=(LineRange(start=1, end=2),)),))
    store.record_diff(run_pk, diff)
    store.record_diff(run_pk, diff)
    count = store.connection.execute("SELECT COUNT(*) AS n FROM diff_hunks").fetchone()["n"]
    assert count == 1


def test_parse_warnings_are_persisted_not_only_logged(store: RunStore) -> None:
    """A half-truncated run must be visibly degraded in the report."""
    run_pk = store.record_run(metadata())
    store.record_warnings(
        run_pk,
        [
            ParseWarning(origin="a.xml", reason="malformed_xml", detail="unclosed token"),
            ParseWarning(origin="b.xml", reason="empty_file"),
        ],
    )
    stored = store.warnings_for_run(run_pk)
    assert [w.reason for w in stored] == ["malformed_xml", "empty_file"]
    assert stored[0].detail == "unclosed token"
