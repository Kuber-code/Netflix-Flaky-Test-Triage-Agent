"""Connection management and migration application."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flaketriage.obs import get_logger
from flaketriage.store.schema import MIGRATIONS, SCHEMA_VERSION

log = get_logger(__name__)

IN_MEMORY = Path(":memory:")


class SchemaTooNewError(RuntimeError):
    """The store was written by a newer version of flaketriage.

    Migrations are forward-only, so downgrading would either lose data or
    misread it. Failing loudly is the only safe response.
    """


def connect(path: Path, *, migrate: bool = True) -> sqlite3.Connection:
    """Open the run store, creating and migrating it if needed."""
    if path != IN_MEMORY:
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row

    # foreign_keys is off by default in SQLite and must be set per connection;
    # without it the ON DELETE CASCADE declarations are decorative.
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if path != IN_MEMORY:
        # WAL lets a long-running detect query coexist with an ingest writer,
        # which is the realistic concurrency pattern for a CI job.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")

    if migrate:
        apply_migrations(connection)
    return connection


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Bring the schema up to :data:`SCHEMA_VERSION`. Returns the new version."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])

    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"store schema is version {current}, this build understands {SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        return current

    for version in range(current + 1, SCHEMA_VERSION + 1):
        # executescript issues an implicit COMMIT before it runs, so the
        # transaction has to live inside the script rather than around it.
        # user_version is bumped in the same transaction as the DDL, so a crash
        # mid-migration cannot leave a half-built schema marked as complete.
        connection.executescript(
            f"BEGIN;\n{MIGRATIONS[version - 1]}\nPRAGMA user_version = {version};\nCOMMIT;"
        )
        log.info("schema_migrated", from_version=version - 1, to_version=version)

    return SCHEMA_VERSION


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction, since isolation_level=None disables implicit ones."""
    connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
