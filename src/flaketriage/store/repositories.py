"""Repository over the run store.

All SQL lives here. Higher layers deal in domain objects, which is what makes
the "move ``executions`` to a columnar store" migration path in
:mod:`flaketriage.store.schema` a change to one file rather than a rewrite.

Writes are idempotent. CI retries ingest steps, workflows get re-run, and an
engineer will run the same command twice; ingesting the same ``(run_id,
attempt, shard)`` a second time must not double a test's observation count,
because observation counts feed flake rate.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from flaketriage.models import (
    CauseCode,
    Classification,
    DiffSummary,
    DowngradeReason,
    Outcome,
    ParseWarning,
    RunMetadata,
    TestCaseResult,
    TestIdentity,
)
from flaketriage.obs import get_logger
from flaketriage.obs.metrics import (
    CallMetric,
    MetricsSummary,
    record_calls,
    record_classifications,
    summarize,
)
from flaketriage.policy.quarantine import QuarantineRecommendation, QuarantineState
from flaketriage.policy.records import (
    QuarantineRecord,
    close,
    expire_overdue,
    list_quarantines,
    open_quarantine_ids,
    record_recommendation,
)
from flaketriage.store.db import connect, transaction

log = get_logger(__name__)


class ExecutionRecord:
    """One persisted execution, joined with the run context the detector needs."""

    __slots__ = (
        "attempt",
        "branch",
        "commit_sha",
        "duration_ms",
        "execution_id",
        "failure_message",
        "failure_type",
        "identity_id",
        "outcome",
        "rerun_observed",
        "run_id",
        "shard_id",
        "stack_trace",
        "started_at",
    )

    def __init__(self, row: sqlite3.Row) -> None:
        self.execution_id: int = row["id"]
        self.identity_id: int = row["identity_id"]
        self.outcome = Outcome(row["outcome"])
        self.duration_ms: int | None = row["duration_ms"]
        self.failure_type: str | None = row["failure_type"]
        self.failure_message: str | None = row["failure_message"]
        self.stack_trace: str | None = row["stack_trace"]
        self.rerun_observed: bool = bool(row["rerun_observed"])
        self.run_id: str = row["run_id"]
        self.attempt: int = row["attempt"]
        self.commit_sha: str = row["commit_sha"]
        self.branch: str = row["branch"]
        self.shard_id: str = row["shard_id"]
        self.started_at: str = row["started_at"]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ExecutionRecord(id={self.execution_id}, identity={self.identity_id}, "
            f"outcome={self.outcome.value}, sha={self.commit_sha[:8]}, attempt={self.attempt})"
        )


class IdentityGroup:
    """One logical test: an identity plus everything aliased to it.

    ``merged_uncertain`` is true when any edge in the group was inferred from a
    similarity score rather than being certain. It travels with the group so that
    a flake rate computed over merged history can be labelled as such instead of
    being presented with false precision.
    """

    __slots__ = ("identity_ids", "merged_uncertain")

    def __init__(self, identity_ids: tuple[int, ...], merged_uncertain: bool) -> None:
        self.identity_ids = identity_ids
        self.merged_uncertain = merged_uncertain

    @property
    def is_merged(self) -> bool:
        return len(self.identity_ids) > 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IdentityGroup(ids={self.identity_ids}, merged_uncertain={self.merged_uncertain})"


class IngestSummary:
    """What one ingest invocation did. Reported to the user, not just logged."""

    __slots__ = (
        "aliases_recorded",
        "cases_ingested",
        "cases_skipped_duplicate",
        "diff_files",
        "new_identities",
        "run_pk",
        "uncertain_aliases",
        "warnings",
    )

    def __init__(
        self,
        run_pk: int,
        cases_ingested: int,
        cases_skipped_duplicate: int,
        new_identities: int,
        diff_files: int,
        warnings: tuple[ParseWarning, ...],
        aliases_recorded: int = 0,
        uncertain_aliases: int = 0,
    ) -> None:
        self.run_pk = run_pk
        self.cases_ingested = cases_ingested
        self.cases_skipped_duplicate = cases_skipped_duplicate
        self.new_identities = new_identities
        self.diff_files = diff_files
        self.warnings = warnings
        self.aliases_recorded = aliases_recorded
        self.uncertain_aliases = uncertain_aliases


class RunStore:
    """Read/write access to the run store."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> RunStore:
        return cls(connect(path))

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record_run(self, metadata: RunMetadata) -> int:
        """Insert or fetch the run row. Returns its primary key.

        Re-ingesting the same ``(run_id, attempt, shard)`` returns the existing
        key instead of creating a second run, so retried CI steps cannot inflate
        observation counts.
        """
        now = _utc_now_iso()
        with transaction(self._connection) as connection:
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, attempt, commit_sha, branch, shard_id, worker_id,
                     started_at, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, attempt, shard_id) DO NOTHING
                """,
                (
                    metadata.run_id,
                    metadata.attempt,
                    metadata.commit_sha,
                    metadata.branch or "",
                    metadata.shard_id or "",
                    metadata.worker_id or "",
                    metadata.started_at.astimezone(UTC).isoformat(),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM runs WHERE run_id = ? AND attempt = ? AND shard_id = ?",
                (metadata.run_id, metadata.attempt, metadata.shard_id or ""),
            ).fetchone()
        return int(row["id"])

    def upsert_identity(self, identity: TestIdentity) -> tuple[int, bool]:
        """Insert or refresh a test identity. Returns ``(id, was_created)``."""
        now = _utc_now_iso()
        with transaction(self._connection) as connection:
            existing = connection.execute(
                "SELECT id FROM test_identities WHERE fingerprint = ?",
                (identity.fingerprint,),
            ).fetchone()

            if existing is not None:
                connection.execute(
                    """
                    UPDATE test_identities
                       SET last_seen = ?,
                           file_path = COALESCE(?, file_path)
                     WHERE id = ?
                    """,
                    (now, identity.file_path, existing["id"]),
                )
                return int(existing["id"]), False

            cursor = connection.execute(
                """
                INSERT INTO test_identities
                    (fingerprint, suite_path, test_name, parameters, file_path,
                     first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.fingerprint,
                    identity.suite_path,
                    identity.test_name,
                    identity.parameters,
                    identity.file_path,
                    now,
                    now,
                ),
            )
        return int(cursor.lastrowid or 0), True

    def record_executions(
        self, run_pk: int, rows: Sequence[tuple[int, TestCaseResult]]
    ) -> tuple[int, int]:
        """Persist executions for a run. Returns ``(inserted, skipped)``.

        ``UNIQUE (run_pk, identity_id)`` plus ``DO NOTHING`` is what makes
        re-ingest safe. A skipped row is normal, not an error.
        """
        now = _utc_now_iso()
        inserted = 0
        with transaction(self._connection) as connection:
            for identity_id, case in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO executions
                        (run_pk, identity_id, outcome, duration_ms, failure_type,
                         failure_message, stack_trace, stdout, stderr,
                         rerun_observed, source_file, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_pk, identity_id) DO NOTHING
                    """,
                    (
                        run_pk,
                        identity_id,
                        case.outcome.value,
                        case.duration_ms,
                        case.failure_type,
                        case.failure_message,
                        case.stack_trace,
                        case.stdout,
                        case.stderr,
                        int(case.rerun_observed),
                        case.source_file,
                        now,
                    ),
                )
                inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        return inserted, len(rows) - inserted

    def record_diff(self, run_pk: int, diff: DiffSummary) -> int:
        """Persist the diff for a run. Returns the number of files recorded."""
        with transaction(self._connection) as connection:
            for change in diff.files:
                cursor = connection.execute(
                    """
                    INSERT INTO diff_files (run_pk, path, old_path, change_type, binary)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (run_pk, path) DO NOTHING
                    """,
                    (
                        run_pk,
                        change.path,
                        change.old_path,
                        change.change_type.value,
                        int(change.binary),
                    ),
                )
                # lastrowid is stale rather than empty when DO NOTHING fires, so
                # rowcount is the only reliable "did we insert" signal here.
                if cursor.rowcount < 1:
                    continue
                diff_file_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO diff_hunks (diff_file_id, side, start_line, end_line)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (diff_file_id, side, line_range.start, line_range.end)
                        for side, ranges in (
                            ("new", change.new_ranges),
                            ("old", change.old_ranges),
                        )
                        for line_range in ranges
                    ],
                )
        return len(diff.files)

    def record_alias(
        self,
        old_identity_id: int,
        new_identity_id: int,
        *,
        similarity: float,
        certain: bool,
        run_pk: int | None = None,
    ) -> bool:
        """Record that two identities are the same logical test.

        Returns whether a new edge was created. An existing edge is upgraded from
        uncertain to certain if better evidence arrives, but never downgraded:
        once a merge has been confirmed, a later weaker observation should not
        cast doubt on it.
        """
        now = _utc_now_iso()
        with transaction(self._connection) as connection:
            cursor = connection.execute(
                """
                INSERT INTO identity_aliases
                    (old_identity_id, new_identity_id, similarity, certain,
                     observed_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (old_identity_id, new_identity_id) DO UPDATE
                    SET certain = MAX(certain, excluded.certain),
                        similarity = MAX(similarity, excluded.similarity)
                """,
                (old_identity_id, new_identity_id, similarity, int(certain), run_pk, now),
            )
            return cursor.rowcount == 1

    def identity_group(self, identity_id: int) -> IdentityGroup:
        """Transitive closure of alias edges touching ``identity_id``.

        Traversal runs in Python over a table that holds one row per observed
        rename -- orders of magnitude smaller than ``executions`` -- which keeps
        the query literal and avoids a recursive CTE for no measurable gain.
        """
        seen = {identity_id}
        frontier = [identity_id]
        uncertain = False

        while frontier:
            current = frontier.pop()
            rows = self._connection.execute(
                """
                SELECT old_identity_id AS old_id, new_identity_id AS new_id, certain
                  FROM identity_aliases
                 WHERE old_identity_id = ? OR new_identity_id = ?
                """,
                (current, current),
            ).fetchall()
            for row in rows:
                if not row["certain"]:
                    uncertain = True
                for neighbour in (int(row["old_id"]), int(row["new_id"])):
                    if neighbour not in seen:
                        seen.add(neighbour)
                        frontier.append(neighbour)

        return IdentityGroup(identity_ids=tuple(sorted(seen)), merged_uncertain=uncertain)

    def executions_for_group(
        self, identity_id: int, *, limit: int | None = None
    ) -> list[ExecutionRecord]:
        """Merged history for one logical test, newest first.

        This is the read the detector uses. Going through the group rather than
        the raw identity is what makes a rename stop resetting a flake rate.
        """
        group = self.identity_group(identity_id)
        records: list[ExecutionRecord] = []
        for member in group.identity_ids:
            records.extend(self.executions_for_identity(member, limit=limit))
        records.sort(key=lambda record: (record.started_at, record.execution_id), reverse=True)
        return records if limit is None else records[:limit]

    def all_identity_ids(self) -> list[int]:
        rows = self._connection.execute("SELECT id FROM test_identities ORDER BY id")
        return [int(row["id"]) for row in rows]

    def identity_ids_for_run(self, run_pk: int) -> list[int]:
        rows = self._connection.execute(
            "SELECT DISTINCT identity_id FROM executions WHERE run_pk = ? ORDER BY identity_id",
            (run_pk,),
        )
        return [int(row["identity_id"]) for row in rows]

    def identities_before_run(self, run_pk: int) -> list[tuple[int, TestIdentity]]:
        """Identities observed strictly before ``run_pk``, newest observation first.

        "Before" is by run start time rather than by insertion order, because
        sharded pipelines ingest out of order and a shard that reports late must
        not look like a later run.
        """
        rows = self._connection.execute(
            """
            SELECT i.id, i.fingerprint, i.suite_path, i.test_name, i.parameters, i.file_path,
                   MAX(r.started_at) AS last_started
              FROM executions e
              JOIN runs r ON r.id = e.run_pk
              JOIN test_identities i ON i.id = e.identity_id
             WHERE r.started_at < (SELECT started_at FROM runs WHERE id = ?)
             GROUP BY i.id
             ORDER BY last_started DESC, i.id
            """,
            (run_pk,),
        )
        return [(int(row["id"]), _identity_from_row(row)) for row in rows]

    def record_metrics(
        self,
        calls: Sequence[CallMetric],
        classifications: dict[int, Classification],
        *,
        run_pk: int | None = None,
        commit_sha: str = "",
        cache_hits: frozenset[int] = frozenset(),
    ) -> tuple[int, int]:
        """Persist one triage invocation's model calls and classifications.

        Written in one transaction so that a crash cannot leave classifications
        recorded with no cost attached to them, which would understate spend.
        """
        with transaction(self._connection) as connection:
            recorded_calls = record_calls(connection, calls, run_pk=run_pk)
            recorded_classifications = record_classifications(
                connection,
                classifications,
                run_pk=run_pk,
                commit_sha=commit_sha,
                cache_hits=cache_hits,
            )
        return recorded_calls, recorded_classifications

    # -- quarantine ---------------------------------------------------------

    def record_quarantines(self, recommendations: Sequence[QuarantineRecommendation]) -> int:
        """Persist recommendations. Duplicates are skipped, not errors."""
        recorded = 0
        with transaction(self._connection) as connection:
            for recommendation in recommendations:
                if record_recommendation(connection, recommendation) is not None:
                    recorded += 1
        return recorded

    def open_quarantine_ids(self) -> frozenset[int]:
        return open_quarantine_ids(self._connection)

    def quarantines(self, *, open_only: bool = True) -> list[QuarantineRecord]:
        return list_quarantines(self._connection, open_only=open_only)

    def expire_overdue_quarantines(self) -> list[QuarantineRecord]:
        with transaction(self._connection) as connection:
            return expire_overdue(connection)

    def release_quarantine(self, record_id: int, *, clean_runs: int) -> bool:
        with transaction(self._connection) as connection:
            return close(
                connection,
                record_id,
                state=QuarantineState.RELEASED,
                reason=f"{clean_runs} consecutive clean execution(s)",
            )

    def recent_outcomes(self, identity_id: int, *, limit: int = 50) -> list[bool]:
        """Newest-first pass/fail flags for a test, for the de-quarantine check.

        Skips are omitted rather than counted as clean: a test that did not run
        has not earned its way out of quarantine.
        """
        return [
            record.outcome.is_pass
            for record in self.executions_for_group(identity_id, limit=limit)
            if record.outcome.counts_as_observation
        ]

    def metrics_summary(self, *, since: str | None = None) -> MetricsSummary:
        return summarize(self._connection, since=since)

    def latest_classification(self, identity_id: int) -> Classification | None:
        """Most recent stored classification for a test, for the policy engine."""
        row = self._connection.execute(
            """
            SELECT cause, confidence, abstained, downgrade_reason, reasoning,
                   evidence, suggested_action, model, prompt_version
              FROM classifications
             WHERE identity_id = ?
             ORDER BY id DESC
             LIMIT 1
            """,
            (identity_id,),
        ).fetchone()
        if row is None:
            return None
        evidence = tuple(json.loads(row["evidence"])) if row["evidence"] else ()
        return Classification(
            cause=CauseCode(row["cause"]),
            confidence=float(row["confidence"]),
            reasoning=row["reasoning"] or "",
            evidence=evidence,
            suggested_action=row["suggested_action"] or "",
            abstained=bool(row["abstained"]),
            downgrade_reason=DowngradeReason(row["downgrade_reason"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
        )

    def record_warnings(self, run_pk: int | None, warnings: Sequence[ParseWarning]) -> int:
        now = _utc_now_iso()
        with transaction(self._connection) as connection:
            connection.executemany(
                """
                INSERT INTO parse_warnings (run_pk, origin, reason, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(run_pk, w.origin, w.reason, w.detail, now) for w in warnings],
            )
        return len(warnings)

    # -- reads -------------------------------------------------------------

    def identity_by_fingerprint(self, value: str) -> TestIdentity | None:
        row = self._connection.execute(
            """
            SELECT fingerprint, suite_path, test_name, parameters, file_path
              FROM test_identities WHERE fingerprint = ?
            """,
            (value,),
        ).fetchone()
        return _identity_from_row(row) if row is not None else None

    def identity_id_by_fingerprint(self, value: str) -> int | None:
        row = self._connection.execute(
            "SELECT id FROM test_identities WHERE fingerprint = ?", (value,)
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def identity_by_id(self, identity_id: int) -> TestIdentity | None:
        row = self._connection.execute(
            """
            SELECT fingerprint, suite_path, test_name, parameters, file_path
              FROM test_identities WHERE id = ?
            """,
            (identity_id,),
        ).fetchone()
        return _identity_from_row(row) if row is not None else None

    def count_identities(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM test_identities").fetchone()
        return int(row["n"])

    def count_executions(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM executions").fetchone()
        return int(row["n"])

    def executions_for_identity(
        self, identity_id: int, *, limit: int | None = None
    ) -> list[ExecutionRecord]:
        """Most recent executions first. ``limit`` bounds the sliding window.

        SQLite reads a negative LIMIT as unbounded, which keeps this a single
        literal statement instead of one assembled at runtime.
        """
        rows = self._connection.execute(
            """
            SELECT e.id, e.identity_id, e.outcome, e.duration_ms, e.failure_type,
                   e.failure_message, e.stack_trace, e.rerun_observed,
                   r.run_id, r.attempt, r.commit_sha, r.branch, r.shard_id, r.started_at
              FROM executions e
              JOIN runs r ON r.id = e.run_pk
             WHERE e.identity_id = ?
             ORDER BY r.started_at DESC, e.id DESC
             LIMIT ?
            """,
            (identity_id, -1 if limit is None else limit),
        )
        return [ExecutionRecord(row) for row in rows]

    def executions_for_run(self, run_pk: int) -> list[ExecutionRecord]:
        rows = self._connection.execute(
            """
            SELECT e.id, e.identity_id, e.outcome, e.duration_ms, e.failure_type,
                   e.failure_message, e.stack_trace, e.rerun_observed,
                   r.run_id, r.attempt, r.commit_sha, r.branch, r.shard_id, r.started_at
              FROM executions e
              JOIN runs r ON r.id = e.run_pk
             WHERE e.run_pk = ?
             ORDER BY e.id
            """,
            (run_pk,),
        )
        return [ExecutionRecord(row) for row in rows]

    def failing_identities_at_sha(self, commit_sha: str) -> list[int]:
        """Identities with at least one non-passing outcome at a commit."""
        rows = self._connection.execute(
            """
            SELECT DISTINCT e.identity_id AS identity_id
              FROM executions e
              JOIN runs r ON r.id = e.run_pk
             WHERE r.commit_sha = ? AND e.outcome IN ('fail', 'error')
             ORDER BY e.identity_id
            """,
            (commit_sha,),
        )
        return [int(row["identity_id"]) for row in rows]

    def warnings_for_run(self, run_pk: int) -> list[ParseWarning]:
        rows = self._connection.execute(
            "SELECT origin, reason, detail FROM parse_warnings WHERE run_pk = ? ORDER BY id",
            (run_pk,),
        )
        return [
            ParseWarning(origin=row["origin"], reason=row["reason"], detail=row["detail"])
            for row in rows
        ]

    def diff_paths_for_sha(self, commit_sha: str) -> frozenset[str]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT d.path AS path
              FROM diff_files d
              JOIN runs r ON r.id = d.run_pk
             WHERE r.commit_sha = ?
            """,
            (commit_sha,),
        )
        return frozenset(str(row["path"]) for row in rows)


def _identity_from_row(row: sqlite3.Row) -> TestIdentity:
    return TestIdentity(
        fingerprint=row["fingerprint"],
        suite_path=row["suite_path"],
        test_name=row["test_name"],
        parameters=row["parameters"],
        file_path=row["file_path"],
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
