"""Shared test builders.

``ExecutionRecord`` reads from a ``sqlite3.Row`` because that is the only shape
the store ever produces. Rather than loosen that contract for tests, the builder
here manufactures a real row, so the tests exercise the same column names and
type coercions that production does.
"""

from __future__ import annotations

import sqlite3

from flaketriage.detect.history import History, build_history
from flaketriage.models import Outcome
from flaketriage.store.repositories import ExecutionRecord


def record(
    *,
    outcome: Outcome = Outcome.FAIL,
    sha: str = "sha1",
    attempt: int = 1,
    started_at: str = "2026-07-20T10:00:00+00:00",
    branch: str = "main",
    shard: str = "",
    rerun: bool = False,
    failure_type: str | None = None,
    failure_message: str | None = None,
    stack_trace: str | None = None,
    execution_id: int = 1,
) -> ExecutionRecord:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT ? AS id, 1 AS identity_id, ? AS outcome, NULL AS duration_ms,
               ? AS failure_type, ? AS failure_message, ? AS stack_trace,
               ? AS rerun_observed, 'run' AS run_id, ? AS attempt, ? AS commit_sha,
               ? AS branch, ? AS shard_id, ? AS started_at
        """,
        (
            execution_id,
            outcome.value,
            failure_type,
            failure_message,
            stack_trace,
            int(rerun),
            attempt,
            sha,
            branch,
            shard,
            started_at,
        ),
    ).fetchone()
    connection.close()
    return ExecutionRecord(row)


PASS: list[Outcome] = [Outcome.PASS]
FAIL: list[Outcome] = [Outcome.FAIL]
MIXED: list[Outcome] = [Outcome.PASS, Outcome.FAIL]
SKIP: list[Outcome] = [Outcome.SKIP]


def history_from(
    pattern: list[tuple[str, list[Outcome]]],
    *,
    branch: str = "main",
    attempts: bool = False,
    stack_trace: str | None = None,
) -> History:
    """Build a history from ``[(sha, [outcomes])]`` in chronological order.

    ``attempts=True`` spreads a commit's outcomes across retry attempts;
    otherwise they are spread across shards. The distinction matters because only
    the former is cross-attempt divergence.
    """
    records = []
    execution_id = 0
    for index, (sha, outcomes) in enumerate(pattern):
        for position, outcome in enumerate(outcomes):
            execution_id += 1
            records.append(
                record(
                    outcome=outcome,
                    sha=sha,
                    attempt=position + 1 if attempts else 1,
                    shard="" if attempts else str(position),
                    started_at=f"2026-07-20T{index:02d}:00:00+00:00",
                    branch=branch,
                    execution_id=execution_id,
                    stack_trace=stack_trace if outcome.is_failure else None,
                )
            )
    return build_history(records)
