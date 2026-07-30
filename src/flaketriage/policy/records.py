"""Persisted quarantine records and their lifecycle.

The lifecycle is the point of this module rather than the storage. A quarantine
moves ``recommended -> active -> (expired | released)``, and the two terminal
states mean different things to a reader: *released* is the system working -- the
test earned its way back -- while *expired* means the TTL ran out with the test
still unstable, and somebody has to decide what to do. Collapsing them into one
"closed" state would hide the only distinction that matters.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from flaketriage.models import CauseCode, Frozen
from flaketriage.policy.ownership import OwnerSource
from flaketriage.policy.quarantine import (
    QuarantineRecommendation,
    QuarantineState,
    is_expired,
    is_expiring,
)
from flaketriage.store.db import transaction


class QuarantineRecord(Frozen):
    """A stored quarantine, as `policy --show-quarantine` reports it."""

    record_id: int
    identity_id: int
    test: str
    state: QuarantineState
    cause: CauseCode = CauseCode.UNKNOWN
    reason: str = ""
    flake_rate: float = 0.0
    observations: int = 0
    owner: str = ""
    owner_source: OwnerSource = OwnerSource.UNRESOLVED
    ttl_days: int = 0
    clean_runs_required: int = 0
    created_at: str = ""
    expires_at: str = ""
    closed_at: str | None = None
    close_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state.is_open

    def expiring(self, *, within_days: int = 3, now: datetime | None = None) -> bool:
        return self.is_open and is_expiring(self.expires_at, within_days=within_days, now=now)

    def expired(self, *, now: datetime | None = None) -> bool:
        return self.is_open and is_expired(self.expires_at, now=now)


def record_recommendation(
    connection: sqlite3.Connection,
    recommendation: QuarantineRecommendation,
    *,
    state: QuarantineState = QuarantineState.RECOMMENDED,
) -> int | None:
    """Store a recommendation. Returns the row id, or ``None`` if one was open.

    The conflict is expected rather than exceptional: a retried CI job re-runs
    triage over the same history and re-derives the same recommendation. The
    partial unique index makes the second attempt a no-op instead of a duplicate.
    """
    if not recommendation.recommended:
        return None

    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT INTO quarantines
            (identity_id, state, cause, reason, flake_rate, observations, owner,
             owner_source, ttl_days, clean_runs_required, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            recommendation.identity_id,
            state.value,
            recommendation.cause.value,
            recommendation.summary(),
            recommendation.flake_rate,
            recommendation.observations,
            recommendation.owner,
            recommendation.owner_source.value,
            recommendation.ttl_days,
            recommendation.clean_runs_required,
            now,
            recommendation.expires_at,
        ),
    )
    if cursor.rowcount < 1:
        return None
    return int(cursor.lastrowid or 0)


def open_quarantine_ids(connection: sqlite3.Connection) -> frozenset[int]:
    rows = connection.execute(
        """
        SELECT DISTINCT identity_id FROM quarantines
         WHERE state IN ('recommended', 'active')
        """
    )
    return frozenset(int(row["identity_id"]) for row in rows)


def list_quarantines(
    connection: sqlite3.Connection, *, open_only: bool = True
) -> list[QuarantineRecord]:
    sql = """
        SELECT q.id, q.identity_id, q.state, q.cause, q.reason, q.flake_rate,
               q.observations, q.owner, q.owner_source, q.ttl_days,
               q.clean_runs_required, q.created_at, q.expires_at, q.closed_at,
               q.close_reason,
               i.suite_path, i.test_name, i.parameters
          FROM quarantines q
          JOIN test_identities i ON i.id = q.identity_id
    """
    if open_only:
        sql += " WHERE q.state IN ('recommended', 'active')"
    sql += " ORDER BY q.expires_at, q.id"
    return [_record_from_row(row) for row in connection.execute(sql)]


def close(
    connection: sqlite3.Connection,
    record_id: int,
    *,
    state: QuarantineState,
    reason: str,
) -> bool:
    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        UPDATE quarantines
           SET state = ?, closed_at = ?, close_reason = ?
         WHERE id = ? AND state IN ('recommended', 'active')
        """,
        (state.value, now, reason, record_id),
    )
    return cursor.rowcount > 0


def expire_overdue(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> list[QuarantineRecord]:
    """Close quarantines whose TTL has passed. Returns what was expired.

    Expiry is applied when the store is read rather than by a background job:
    there is no daemon, so a TTL that only advanced while something was running
    would never advance at all.
    """
    moment = now or datetime.now(UTC)
    overdue = [record for record in list_quarantines(connection) if record.expired(now=moment)]
    for record in overdue:
        close(
            connection,
            record.record_id,
            state=QuarantineState.EXPIRED,
            reason=f"ttl of {record.ttl_days} day(s) elapsed while still unstable",
        )
    return overdue


class QuarantineStore:
    """Quarantine persistence, owned by the policy layer.

    Deliberately *not* methods on ``RunStore``. Putting them there created a
    circular import -- ``policy`` needs ``detect``, ``detect`` needs ``store``, and
    ``store`` then needed ``policy`` for the type annotations. It happened to work
    when the modules were imported in one particular order through the CLI and blew
    up the moment anything imported ``flaketriage.policy`` first.

    The direction that is allowed is ``policy -> store``: the policy layer knows
    about storage, storage knows nothing about policy. So the SQL lives here, next
    to the rules it serves, and takes a connection it does not own.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(self, recommendations: Sequence[QuarantineRecommendation]) -> int:
        """Persist recommendations. Duplicates are skipped, not errors."""
        recorded = 0
        with transaction(self._connection) as connection:
            for recommendation in recommendations:
                if record_recommendation(connection, recommendation) is not None:
                    recorded += 1
        return recorded

    def open_ids(self) -> frozenset[int]:
        return open_quarantine_ids(self._connection)

    def records(self, *, open_only: bool = True) -> list[QuarantineRecord]:
        return list_quarantines(self._connection, open_only=open_only)

    def expire_overdue(self) -> list[QuarantineRecord]:
        with transaction(self._connection) as connection:
            return expire_overdue(connection)

    def release(self, record_id: int, *, clean_runs: int) -> bool:
        with transaction(self._connection) as connection:
            return close(
                connection,
                record_id,
                state=QuarantineState.RELEASED,
                reason=f"{clean_runs} consecutive clean execution(s)",
            )


def _record_from_row(row: sqlite3.Row) -> QuarantineRecord:
    base = f"{row['suite_path']}::{row['test_name']}" if row["suite_path"] else row["test_name"]
    display = f"{base}[{row['parameters']}]" if row["parameters"] else base
    return QuarantineRecord(
        record_id=int(row["id"]),
        identity_id=int(row["identity_id"]),
        test=display,
        state=QuarantineState(row["state"]),
        cause=CauseCode(row["cause"]),
        reason=row["reason"] or "",
        flake_rate=float(row["flake_rate"]),
        observations=int(row["observations"]),
        owner=row["owner"] or "",
        owner_source=OwnerSource(row["owner_source"]),
        ttl_days=int(row["ttl_days"]),
        clean_runs_required=int(row["clean_runs_required"]),
        created_at=row["created_at"] or "",
        expires_at=row["expires_at"] or "",
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
    )


def summarize_states(records: Sequence[QuarantineRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.state.value] = counts.get(record.state.value, 0) + 1
    return counts
